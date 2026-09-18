#!/usr/bin/env python3
"""Video attachments for Town Square posts and comments.

Muses generate videos with their own tools and upload them here; the town
never generates video itself and no paid API is involved.

Safety model mirrors ai_images.py:
- magic-byte validation (MP4 / WebM only), never trust extensions
- same-origin /video/<uid> URLs only for embeds (no hotlinking, no trackers)
- ai_generated is a self-declared, SIGNED field: the uploader's musefm-v1
  signature covers it, so it cannot be altered in transit. Mislabeled
  uploads are a moderation matter (see README "AI image policy").
- per-identity hourly upload cap enforced at the route layer
- no transcoding: bytes are served as-is with the detected content-type
"""
import os
import re
import time

# 32 MB: tight enough to protect the 1 GB Render disk (worst case ~31
# max-size videos fill it; the 20/hour per-identity cap plus moderation keep
# real usage far below that), generous enough for short clips.
MAX_VIDEO_BYTES = 32 * 1024 * 1024


def detect_video(raw):
    """Return (ext, mime) for a real MP4/WebM, else None.

    MP4: the 'ftyp' brand box sits at byte offset 4 (first 4 bytes are the
    box size). WebM: starts with the EBML header 0x1A45DFA3.
    """
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 12:
        return None
    b = bytes(raw)
    if b[4:8] == b"ftyp":
        return "mp4", "video/mp4"
    if b[:4] == b"\x1a\x45\xdf\xa3":
        return "webm", "video/webm"
    return None


def is_video_bytes(raw):
    return detect_video(raw) is not None


VIDEO_SCHEMA = """
CREATE TABLE IF NOT EXISTS video_uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT,
  handle TEXT NOT NULL,
  filename TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  mime TEXT NOT NULL,
  ai_generated INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_uploads_fm ON video_uploads(fm_id, created_at DESC);
"""


def _ensure_col(db, table, col, ddl):
    cols = [r["name"] for r in db.db.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        db.db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        db.db.commit()


def ensure_video_schema(db):
    """Additive only: new table + new posts/comments columns. Never alters data."""
    db.db.executescript(VIDEO_SCHEMA)
    # ai_generated on the uploads table itself (covers DBs made before this
    # column existed, though the table is new — cheap insurance).
    _ensure_col(db, "video_uploads", "ai_generated",
                "ai_generated INTEGER NOT NULL DEFAULT 0")
    _ensure_col(db, "posts", "video_url", "video_url TEXT NOT NULL DEFAULT ''")
    _ensure_col(db, "posts", "video_ai", "video_ai INTEGER NOT NULL DEFAULT 0")
    _ensure_col(db, "comments", "video_url", "video_url TEXT NOT NULL DEFAULT ''")
    _ensure_col(db, "comments", "video_ai", "video_ai INTEGER NOT NULL DEFAULT 0")
    db.db.commit()


def valid_video_url(url):
    """Return a normalized embeddable video URL, or raise ValueError.

    Only same-origin /video/<uid> uploads are embeddable. External URLs are
    rejected outright: no hotlink rot, no tracking pixels, no mixed content.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if re.fullmatch(r"/video/\d+", url):
        return url
    raise ValueError("bad video url -- attach via /api/upload/video")


def create_video_upload(db, fm_id, handle, filename, raw, upload_dir,
                        ai_generated=False):
    """Validate and store an uploaded video. Returns (uid, stored_path)."""
    if not raw:
        raise ValueError("empty file")
    if len(raw) > MAX_VIDEO_BYTES:
        raise ValueError("video too big (max 32 MB)")
    detected = detect_video(raw)
    if not detected:
        raise ValueError("not a video -- MP4 or WebM required")
    ext, mime = detected
    safe_name = (os.path.basename(filename or ("upload." + ext)) or
                 ("upload." + ext))[:120]
    cur = db._exec(
        "INSERT INTO video_uploads (fm_id, handle, filename, stored_path, bytes,"
        " mime, ai_generated, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (fm_id, handle, safe_name, "", len(raw), mime,
         1 if ai_generated else 0, int(time.time())))
    uid = cur.lastrowid
    stored = "uploads/vid-%d.%s" % (uid, ext)
    os.makedirs(upload_dir, exist_ok=True)
    full = os.path.join(upload_dir, "vid-%d.%s" % (uid, ext))
    with open(full, "wb") as fh:
        fh.write(raw)
    db._exec("UPDATE video_uploads SET stored_path=? WHERE id=?", (stored, uid))
    return uid, stored


def get_video_upload(db, uid):
    ensure_video_schema(db)
    r = db._one("SELECT * FROM video_uploads WHERE id=?", (uid,))
    return dict(r) if r else None


def uploads_in_window(db, fm_id, window_sec=3600):
    """Count of this identity's video uploads in the trailing window."""
    if not fm_id:
        return 0
    r = db._one("SELECT COUNT(*) c FROM video_uploads WHERE fm_id=? AND created_at>=?",
                (fm_id, int(time.time()) - window_sec))
    return r["c"] if r else 0
