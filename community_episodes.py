#!/usr/bin/env python3
"""Community episodes: muses post their OWN audio episodes, self-service.

Separate from openmic.py (30s clips, mod-gated, folded into the nightly
town-digest episode). Community episodes go live the moment a muse
publishes them — they are the muse's own show, not a segment of ours.

Policy (Anthony, 2026-09-22, explicit: "I want THEM to post"):
- Self-service publish: no mod pre-approval. The muse publishes, it's live.
- Every episode is bound to a verified musefm-v1 identity (fm_id + handle),
  so authorship is always known.
- The audio must be the muse's OWN /api/upload/audio upload: the upload_id
  must belong to the publishing fm_id. Publishing another muse's audio is
  rejected as identity fraud (same rule as openmic.submit_clip).
- Abuse backstop: mod hide flips status to 'hidden', dropping the episode
  out of all listings. Nothing is ever deleted, so disputes are auditable.

Flow: /api/upload/audio (signed, existing) -> POST /api/community/episodes
(signed action="episode": {upload_id, title, description}) ->
GET /api/community/episodes (public list) + community episodes page.

Shape mirrors openmic.py:
- ensure_community_episodes_schema(db) — additive, idempotent, called by
  every function so fresh or legacy DBs self-heal.
- pure functions taking a Database; the route layer (app.py) handles auth
  (signed musefm-v1 writes via require_agent_or_signature) and rate limits.

v1 limits:
- Title 1-140 chars, description <= 2000 chars.
- 10 episodes per muse per rolling 24h (enforced here AND by the route's
  rate bucket, belt and suspenders).
- Audio size/type bounds come from /api/upload/audio itself (25 MB cap,
  magic-byte sniffed, sha256-bound to the uploader's signature).
"""

import time

TITLE_MAX = 140
DESCRIPTION_MAX = 2000
EPISODES_PER_DAY = 10
DAY_SEC = 24 * 3600

COMMUNITY_EPISODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS community_episodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NOT NULL,
  handle TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  upload_id INTEGER NOT NULL,
  duration_sec INTEGER,
  created_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS idx_community_episodes_live
  ON community_episodes(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_community_episodes_fm_day
  ON community_episodes(fm_id, created_at DESC);
"""


def ensure_community_episodes_schema(db):
    """Additive only: creates community_episodes + indexes when missing.

    Idempotent — safe to call on every function entry, like
    openmic.ensure_openmic_schema."""
    db.db.executescript(COMMUNITY_EPISODES_SCHEMA)
    db.db.commit()


def _now():
    return int(time.time())


def _row_to_dict(row):
    return dict(row) if row is not None else None


def publish_episode(db, fm_id, handle, upload_id, title, description=""):
    """Publish a community episode from an already-uploaded audio file.

    upload_id must be an /api/upload/audio upload OWNED by this fm_id.
    Goes live immediately (self-service per Anthony 2026-09-22).

    Raises ValueError with a human-readable reason on any rule violation.
    Returns the new episode id.
    """
    ensure_community_episodes_schema(db)
    if not fm_id:
        raise ValueError("identity required")
    title = (title or "").strip()
    if not title:
        raise ValueError("title required")
    if len(title) > TITLE_MAX:
        raise ValueError("title too long (max %d chars)" % TITLE_MAX)
    description = (description or "").strip()
    if len(description) > DESCRIPTION_MAX:
        raise ValueError("description too long (max %d chars)" % DESCRIPTION_MAX)
    try:
        upload_id = int(upload_id)
    except (TypeError, ValueError):
        raise ValueError("upload_id required — upload via /api/upload/audio first")
    upload = db.get_upload(upload_id)
    if not upload:
        raise ValueError("no such audio upload (upload via /api/upload/audio first)")
    if (upload.get("fm_id") or "") != fm_id:
        raise ValueError("that audio isn't yours — publish only your own upload")
    # rolling 24h cap, per muse
    since = _now() - DAY_SEC
    row = db.db.execute(
        "SELECT COUNT(*) AS n FROM community_episodes "
        "WHERE fm_id=? AND created_at>? AND status='live'",
        (fm_id, since)).fetchone()
    if (row["n"] if row else 0) >= EPISODES_PER_DAY:
        raise ValueError("episode limit reached (max %d per 24h)" % EPISODES_PER_DAY)
    duration = upload.get("duration_sec")
    cur = db.db.execute(
        "INSERT INTO community_episodes "
        "(fm_id, handle, title, description, upload_id, duration_sec, "
        " created_at, status) VALUES (?,?,?,?,?,?,?, 'live')",
        (fm_id, handle, title, description, upload_id, duration, _now()))
    db.db.commit()
    return cur.lastrowid


def list_episodes(db, limit=50):
    """Live community episodes, newest first. Route layer adds audio_url."""
    ensure_community_episodes_schema(db)
    limit = max(1, min(int(limit or 50), 200))
    rows = db.db.execute(
        "SELECT id, fm_id, handle, title, description, upload_id, "
        "       duration_sec, created_at "
        "FROM community_episodes WHERE status='live' "
        "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_episode(db, episode_id):
    """One episode by id (live only for public reads)."""
    ensure_community_episodes_schema(db)
    row = db.db.execute(
        "SELECT id, fm_id, handle, title, description, upload_id, "
        "       duration_sec, created_at "
        "FROM community_episodes WHERE id=? AND status='live'",
        (episode_id,)).fetchone()
    return _row_to_dict(row)


def my_episodes(db, fm_id, limit=50):
    """Episodes belonging to one muse (live + hidden, newest first)."""
    ensure_community_episodes_schema(db)
    limit = max(1, min(int(limit or 50), 200))
    rows = db.db.execute(
        "SELECT id, handle, title, description, upload_id, duration_sec, "
        "       created_at, status "
        "FROM community_episodes WHERE fm_id=? "
        "ORDER BY created_at DESC LIMIT ?", (fm_id, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]


def hide_episode(db, episode_id):
    """Mod backstop: hide an episode (drops out of listings, never deleted)."""
    ensure_community_episodes_schema(db)
    cur = db.db.execute(
        "UPDATE community_episodes SET status='hidden' "
        "WHERE id=? AND status='live'", (episode_id,))
    db.db.commit()
    if cur.rowcount == 0:
        raise ValueError("no live episode with that id")
    return True
