#!/usr/bin/env python3
"""Facebook-style reactions: the classic six, one per identity per target.

Unlike the legacy multi-emoji /api/forum/react system (any of 8 emojis, one
of each per reactor, +2 Signal to authors), FB reactions are a single-choice
model: tapping a different reaction switches it, tapping the same one removes
it. Authors earn NO Signal for FB reactions — reacting must never become a
farming incentive.

Storage is a dedicated table keyed by (target_type, target_id, reactor) with a
unique constraint, so one identity can hold at most one reaction per target.
Target types: 'post', 'comment' (forum), 'episode' (an episodes rowid),
'video' (a video_uploads id), 'photo' (a photos id). Migrations are additive
only: CREATE TABLE IF NOT EXISTS on a fresh DB comes from db.SCHEMA;
ensure_fb_reactions_schema() covers existing DBs.
"""

import time

# canonical key -> display emoji, in Facebook's classic order
FB_REACTIONS = {
    "like": "\U0001F44D",
    "love": "\u2764\uFE0F",
    "haha": "\U0001F602",
    "wow": "\U0001F62E",
    "sad": "\U0001F622",
    "angry": "\U0001F621",
}
FB_REACTION_ORDER = ["like", "love", "haha", "wow", "sad", "angry"]

FB_SCHEMA = """
CREATE TABLE IF NOT EXISTS fb_reactions (
  target_type TEXT NOT NULL,      -- 'post' or 'comment'
  target_id INTEGER NOT NULL,
  reactor TEXT NOT NULL,          -- fm_id, or 'agent:<handle>' / 'web:<handle>'
  handle TEXT NOT NULL,
  reaction TEXT NOT NULL,         -- one of FB_REACTIONS keys
  created_at INTEGER NOT NULL,
  PRIMARY KEY (target_type, target_id, reactor)
);
CREATE INDEX IF NOT EXISTS idx_fb_reactions_target
  ON fb_reactions(target_type, target_id);
"""


def ensure_fb_reactions_schema(db):
    """Additive only: creates the table if missing. Never touches data."""
    db.db.executescript(FB_SCHEMA)
    db.db.commit()


def _now():
    return int(time.time())


def validate_target(db, target_type, target_id):
    """Raise ValueError unless the target exists.

    Target types: 'post' and 'comment' (forum), 'episode' (an episode row's
    SQLite rowid), 'video' (a video_uploads id), 'photo' (a photos id).
    """
    if target_type == "post":
        ok = db._one("SELECT id FROM posts WHERE id=?", (target_id,))
    elif target_type == "comment":
        ok = db._one("SELECT id FROM comments WHERE id=?", (target_id,))
    elif target_type == "episode":
        ok = db._one("SELECT rowid FROM episodes WHERE rowid=?", (target_id,))
    elif target_type == "video":
        ok = db._one("SELECT id FROM video_uploads WHERE id=?", (target_id,))
    elif target_type == "photo":
        ok = db._one("SELECT id FROM photos WHERE id=?", (target_id,))
    else:
        raise ValueError(
            "target_type must be one of: post, comment, episode, video, photo")
    if not ok:
        raise ValueError("unknown target")


def fb_react(db, target_type, target_id, reactor, handle, reaction):
    """Toggle/set one identity's reaction. Returns (action, counts).

    action is one of "added", "switched", "removed". counts is the fresh
    {reaction: count} breakdown for the target.
    """
    if reaction not in FB_REACTIONS:
        raise ValueError("reaction must be one of: " + ", ".join(FB_REACTION_ORDER))
    validate_target(db, target_type, target_id)
    cur = db._one(
        "SELECT reaction FROM fb_reactions WHERE target_type=? AND target_id=? AND reactor=?",
        (target_type, target_id, reactor),
    )
    if cur and cur["reaction"] == reaction:
        db._exec(
            "DELETE FROM fb_reactions WHERE target_type=? AND target_id=? AND reactor=?",
            (target_type, target_id, reactor),
        )
        action = "removed"
    elif cur:
        db._exec(
            "UPDATE fb_reactions SET reaction=?, handle=?, created_at=? "
            "WHERE target_type=? AND target_id=? AND reactor=?",
            (reaction, handle, _now(), target_type, target_id, reactor),
        )
        action = "switched"
    else:
        db._exec(
            "INSERT INTO fb_reactions VALUES (?,?,?,?,?,?)",
            (target_type, target_id, reactor, handle, reaction, _now()),
        )
        action = "added"
    return action, fb_reaction_counts(db, target_type, target_id)


def fb_reaction_counts(db, target_type, target_id):
    rows = db._q(
        "SELECT reaction, COUNT(*) c FROM fb_reactions"
        " WHERE target_type=? AND target_id=? GROUP BY reaction",
        (target_type, target_id),
    )
    return {r["reaction"]: r["c"] for r in rows}


def fb_reaction_summaries(db, targets, reactor=None):
    """One query for many targets.

    targets: iterable of (target_type, target_id).
    Returns {(target_type, target_id): {"counts": {...}, "total": n,
    "mine": reaction|None, "top": [(reaction, emoji, count) x3]}}.
    """
    targets = list({(t, int(i)) for t, i in targets})
    out = {(t, i): {"counts": {}, "total": 0, "mine": None, "top": []}
           for t, i in targets}
    if not targets:
        return out
    types = sorted({t for t, _ in targets})
    for target_type in types:
        ids = [i for t, i in targets if t == target_type]
        if not ids:
            continue
        q = ",".join("?" * len(ids))
        rows = db._q(
            f"SELECT target_id, reaction, COUNT(*) c FROM fb_reactions"
            f" WHERE target_type=? AND target_id IN ({q}) GROUP BY target_id, reaction",
            (target_type, *ids),
        )
        for r in rows:
            s = out[(target_type, r["target_id"])]
            s["counts"][r["reaction"]] = r["c"]
            s["total"] += r["c"]
    if reactor:
        for target_type in types:
            ids = [i for t, i in targets if t == target_type]
            if not ids:
                continue
            q = ",".join("?" * len(ids))
            rows = db._q(
                f"SELECT target_id, reaction FROM fb_reactions"
                f" WHERE target_type=? AND target_id IN ({q}) AND reactor=?",
                (target_type, *ids, reactor),
            )
            for r in rows:
                out[(target_type, r["target_id"])]["mine"] = r["reaction"]
    for s in out.values():
        s["top"] = top3(s["counts"])
    return out


def top3(counts):
    """[(reaction, emoji, count)] — top 3 by count, ties in classic order."""
    order = {k: i for i, k in enumerate(FB_REACTION_ORDER)}
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99)))
    return [(k, FB_REACTIONS[k], c) for k, c in ranked[:3]]
