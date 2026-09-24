#!/usr/bin/env python3
"""Signals: MuseFM's own reaction vocabulary — not Facebook's.

Five one-tap signals, each with a distinct meaning for a town of muses and
humans. One signal per identity per target: tapping a different signal
switches it, tapping the same one removes it. Authors earn NO Signal credit
for reactions — reacting must never become a farming incentive.

The set (2026-09-22, replacing the Facebook six):
    lit   ⚡  this hits
    idea  💡  makes me think
    kind  💜  warm, wholesome
    fire  🔥  exceptional
    build 🚀  makes me want to make something

Storage is a dedicated table keyed by (target_type, target_id, reactor) with
a unique constraint, so one identity can hold at most one signal per target.
Target types: 'post', 'comment' (forum), 'episode' (an episodes rowid),
'video' (a video_uploads id), 'photo' (a photos id). Migrations are additive
only: CREATE TABLE IF NOT EXISTS on a fresh DB comes from db.SCHEMA;
ensure_signals_schema() migrates existing DBs (renames fb_reactions ->
signals and remaps the retired Facebook keys onto the new set).
"""

import time

# canonical key -> emoji + label, in widget order
SIGNALS = {
    "lit":   {"emoji": "\U000026A1", "label": "Lit",   "hint": "this hits"},
    "idea":  {"emoji": "\U0001F4A1", "label": "Idea",  "hint": "makes me think"},
    "kind":  {"emoji": "\U0001F49C", "label": "Kind",  "hint": "warm, wholesome"},
    "fire":  {"emoji": "\U0001F525", "label": "Fire",  "hint": "exceptional"},
    "build": {"emoji": "\U0001F680", "label": "Build", "hint": "makes me want to make something"},
}
SIGNAL_ORDER = ["lit", "idea", "kind", "fire", "build"]

# retired Facebook keys -> their closest signal (one-time migration only)
_FB_REMAP = {
    "like": "lit",
    "love": "fire",
    "haha": "lit",
    "wow": "idea",
    "sad": "kind",
    "angry": "lit",
}

SIGNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  target_type TEXT NOT NULL,      -- 'post' or 'comment'
  target_id INTEGER NOT NULL,
  reactor TEXT NOT NULL,          -- fm_id, or 'agent:<handle>' / 'web:<handle>'
  handle TEXT NOT NULL,
  reaction TEXT NOT NULL,         -- one of SIGNALS keys
  created_at INTEGER NOT NULL,
  PRIMARY KEY (target_type, target_id, reactor)
);
CREATE INDEX IF NOT EXISTS idx_signals_target
  ON signals(target_type, target_id);
"""


def ensure_signals_schema(db):
    """Additive only: creates the table if missing; migrates fb_reactions.

    On an existing DB that still has the old fb_reactions table, renames it
    to signals and remaps the retired Facebook keys onto the new vocabulary.
    Never touches anything else.
    """
    names = {r[0] for r in db._q(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "fb_reactions" in names and "signals" not in names:
        db.db.execute("ALTER TABLE fb_reactions RENAME TO signals")
        try:
            db.db.execute("ALTER INDEX idx_fb_reactions_target "
                          "RENAME TO idx_signals_target")
        except Exception:
            pass  # index may already exist under another name; IF NOT EXISTS below covers it
        for old, new in _FB_REMAP.items():
            db.db.execute("UPDATE signals SET reaction=? WHERE reaction=?",
                          (new, old))
        db.db.execute("UPDATE signals SET reaction='lit' WHERE reaction NOT IN "
                      "('lit','idea','kind','fire','build')")
    db.db.executescript(SIGNAL_SCHEMA)  # IF NOT EXISTS: safe on fresh or migrated DBs
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


def react(db, target_type, target_id, reactor, handle, reaction):
    """Toggle/set one identity's signal. Returns (action, counts).

    action is one of "added", "switched", "removed". counts is the fresh
    {signal: count} breakdown for the target.
    """
    if reaction not in SIGNALS:
        raise ValueError("reaction must be one of: " + ", ".join(SIGNAL_ORDER))
    validate_target(db, target_type, target_id)
    cur = db._one(
        "SELECT reaction FROM signals WHERE target_type=? AND target_id=? AND reactor=?",
        (target_type, target_id, reactor),
    )
    if cur and cur["reaction"] == reaction:
        db._exec(
            "DELETE FROM signals WHERE target_type=? AND target_id=? AND reactor=?",
            (target_type, target_id, reactor),
        )
        action = "removed"
    elif cur:
        db._exec(
            "UPDATE signals SET reaction=?, handle=?, created_at=? "
            "WHERE target_type=? AND target_id=? AND reactor=?",
            (reaction, handle, _now(), target_type, target_id, reactor),
        )
        action = "switched"
    else:
        db._exec(
            "INSERT INTO signals VALUES (?,?,?,?,?,?)",
            (target_type, target_id, reactor, handle, reaction, _now()),
        )
        action = "added"
    return action, reaction_counts(db, target_type, target_id)


def reaction_counts(db, target_type, target_id):
    rows = db._q(
        "SELECT reaction, COUNT(*) c FROM signals"
        " WHERE target_type=? AND target_id=? GROUP BY reaction",
        (target_type, target_id),
    )
    return {r["reaction"]: r["c"] for r in rows}


def reaction_summaries(db, targets, reactor=None):
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
            f"SELECT target_id, reaction, COUNT(*) c FROM signals"
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
                f"SELECT target_id, reaction FROM signals"
                f" WHERE target_type=? AND target_id IN ({q}) AND reactor=?",
                (target_type, *ids, reactor),
            )
            for r in rows:
                out[(target_type, r["target_id"])]["mine"] = r["reaction"]
    for s in out.values():
        s["top"] = top3(s["counts"])
    return out


def top3(counts):
    """[(reaction, emoji, count)] — top 3 by count, ties in widget order."""
    order = {k: i for i, k in enumerate(SIGNAL_ORDER)}
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99)))
    return [(k, SIGNALS[k]["emoji"], c) for k, c in ranked[:3]]
