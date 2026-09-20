#!/usr/bin/env python3
"""
Agentic Memory API — persistent cross-task memory for AI agents.

Workroom pilot agents (pebble, rill, sable) have task-local history only.
This module gives them durable memory across tasks: store / recall /
update / forget / decay memories of kinds fact|preference|outcome|
lesson|goal.

Scoping: each memory belongs to an `owner` — an agent handle, or the
"shared" namespace readable by every pilot agent. Auth/scoping decisions
live in the route layer (app.py); this module enforces nothing beyond
validation and does all writes additively.

Schema (all additive, CREATE TABLE IF NOT EXISTS):
  agent_memories(id INTEGER PK, owner TEXT, kind TEXT, key TEXT,
                 value TEXT, confidence REAL, created_at, updated_at)
      UNIQUE(owner, key)
  agent_memory_events(id INTEGER PK, owner TEXT, action TEXT, key TEXT,
                      created_at)  -- append-only audit log
"""

import re
import time

from db import has_banned

KINDS = ("fact", "preference", "outcome", "lesson", "goal")
SHARED = "shared"
OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MAX_KEY_LEN = 120
MAX_VALUE_LEN = 4000


def _now():
    return int(time.time())


def _clean(s, limit, what):
    s = (s or "").strip()
    if not s:
        raise ValueError(f"{what} is required")
    if len(s) > limit:
        raise ValueError(f"{what} too long (max {limit} chars)")
    return s


def _clean_owner(owner):
    owner = _clean(owner, 40, "owner").lower()
    if not OWNER_RE.match(owner):
        raise ValueError("owner must match [a-z0-9][a-z0-9_-]{0,39}")
    return owner


def _clean_kind(kind):
    kind = _clean(kind, 20, "kind").lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    return kind


def _clean_confidence(conf):
    if conf is None:
        return 1.0
    try:
        c = float(conf)
    except (TypeError, ValueError):
        raise ValueError("confidence must be a number 0..1")
    if not 0.0 <= c <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return c


def ensure_agent_memory_schema(db):
    """Additive only: two memory tables. Safe on fresh and existing DBs."""
    db.db.executescript("""
    CREATE TABLE IF NOT EXISTS agent_memories (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      owner TEXT NOT NULL,
      kind TEXT NOT NULL,
      key TEXT NOT NULL,
      value TEXT NOT NULL,
      confidence REAL NOT NULL DEFAULT 1.0,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      UNIQUE(owner, key)
    );
    CREATE INDEX IF NOT EXISTS idx_mem_owner
      ON agent_memories(owner, updated_at DESC);
    CREATE TABLE IF NOT EXISTS agent_memory_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      owner TEXT NOT NULL,
      action TEXT NOT NULL,
      key TEXT NOT NULL,
      created_at INTEGER NOT NULL
    );
    """)
    db.db.commit()


def _log(db, owner, action, key):
    db.db.execute(
        "INSERT INTO agent_memory_events (owner, action, key, created_at)"
        " VALUES (?, ?, ?, ?)",
        (owner, action, key, _now()))


def _row_to_dict(r):
    return {"id": r["id"], "owner": r["owner"], "kind": r["kind"],
            "key": r["key"], "value": r["value"],
            "confidence": r["confidence"],
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def store_memory(db, owner, kind, key, value, confidence=None):
    """Upsert one memory. Returns the stored memory dict."""
    owner = _clean_owner(owner)
    kind = _clean_kind(kind)
    key = _clean(key, MAX_KEY_LEN, "key")
    value = _clean(value, MAX_VALUE_LEN, "value")
    if has_banned(value):
        raise ValueError("value contains a blocked word")
    confidence = _clean_confidence(confidence)
    now = _now()
    db.db.execute(
        """INSERT INTO agent_memories
             (owner, kind, key, value, confidence, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(owner, key) DO UPDATE SET
             kind=excluded.kind, value=excluded.value,
             confidence=excluded.confidence, updated_at=excluded.updated_at""",
        (owner, kind, key, value, confidence, now, now))
    _log(db, owner, "store", key)
    db.db.commit()
    return get_memory(db, owner, key)


def get_memory(db, owner, key):
    """Fetch one memory by owner+key; None if missing."""
    owner = _clean_owner(owner)
    key = _clean(key, MAX_KEY_LEN, "key")
    r = db.db.execute(
        "SELECT * FROM agent_memories WHERE owner = ? AND key = ?",
        (owner, key)).fetchone()
    return _row_to_dict(r) if r else None


def update_memory(db, owner, key, value=None, confidence=None, kind=None):
    """Update value/confidence/kind of an existing memory.

    Raises LookupError if the memory does not exist.
    Returns the updated memory dict."""
    owner = _clean_owner(owner)
    key = _clean(key, MAX_KEY_LEN, "key")
    existing = get_memory(db, owner, key)
    if existing is None:
        raise LookupError(f"no memory '{key}' for owner '{owner}'")
    sets, args = [], []
    if value is not None:
        value = _clean(value, MAX_VALUE_LEN, "value")
        if has_banned(value):
            raise ValueError("value contains a blocked word")
        sets.append("value = ?")
        args.append(value)
    if confidence is not None:
        sets.append("confidence = ?")
        args.append(_clean_confidence(confidence))
    if kind is not None:
        sets.append("kind = ?")
        args.append(_clean_kind(kind))
    if not sets:
        raise ValueError("nothing to update")
    sets.append("updated_at = ?")
    args.append(_now())
    args.extend([owner, key])
    db.db.execute(
        f"UPDATE agent_memories SET {', '.join(sets)}"
        " WHERE owner = ? AND key = ?", args)
    _log(db, owner, "update", key)
    db.db.commit()
    return get_memory(db, owner, key)


def forget_memory(db, owner, key):
    """Delete one memory. Raises LookupError if missing. Returns True."""
    owner = _clean_owner(owner)
    key = _clean(key, MAX_KEY_LEN, "key")
    cur = db.db.execute(
        "DELETE FROM agent_memories WHERE owner = ? AND key = ?",
        (owner, key))
    if cur.rowcount == 0:
        raise LookupError(f"no memory '{key}' for owner '{owner}'")
    _log(db, owner, "forget", key)
    db.db.commit()
    return True


def recall_memories(db, owner, q="", kind=None, limit=20):
    """Keyword recall: every token in q must appear in key+value.

    Newest first. limit clamped to 1..100."""
    owner = _clean_owner(owner)
    tokens = [t for t in re.split(r"\s+", (q or "").strip().lower()) if t]
    sql = "SELECT * FROM agent_memories WHERE owner = ?"
    args = [owner]
    if kind is not None:
        sql += " AND kind = ?"
        args.append(_clean_kind(kind))
    for t in tokens:
        sql += " AND (lower(key) LIKE ? OR lower(value) LIKE ?)"
        args.extend([f"%{t}%", f"%{t}%"])
    sql += " ORDER BY updated_at DESC LIMIT ?"
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    args.append(max(1, min(100, limit)))
    return [_row_to_dict(r) for r in db.db.execute(sql, args).fetchall()]


def list_memories(db, owner, kind=None, limit=50):
    """Dump a namespace, newest first. limit clamped to 1..200."""
    return recall_memories(db, owner, q="", kind=kind,
                           limit=max(1, min(200, int(limit or 50))))


def decay_memories(db, owner, older_than_days=90, below_confidence=0.4):
    """Prune memories that are BOTH stale and low-confidence.

    Returns the number pruned. Never touches fresh or high-confidence
    memories."""
    owner = _clean_owner(owner)
    try:
        days = float(older_than_days)
    except (TypeError, ValueError):
        raise ValueError("older_than_days must be a number")
    if days < 0:
        raise ValueError("older_than_days must be >= 0")
    cutoff = _now() - int(days * 86400)
    conf = _clean_confidence(below_confidence)
    cur = db.db.execute(
        "DELETE FROM agent_memories"
        " WHERE owner = ? AND updated_at < ? AND confidence < ?",
        (owner, cutoff, conf))
    n = cur.rowcount
    if n:
        _log(db, owner, "decay", f"{n} pruned")
    db.db.commit()
    return n
