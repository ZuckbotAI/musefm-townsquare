"""Race + migration test for dm_seen.seen_msg_id (2026-09-27).

- Same-second send/look race: mark seen, then a message lands in the
  SAME integer second. Old timestamp logic missed it; id logic must not.
- Migration: old-schema dm_seen (no seen_msg_id) backfills exactly from
  seen_at, preserving unread state.
"""
import os, sys, tempfile, time

TMP = tempfile.mkdtemp(prefix="dmrace_")
DBP = os.path.join(TMP, "dm_race.db")
REPO = os.path.dirname(os.path.abspath(__file__))
os.environ["TOWNSQUARE_DB"] = DBP
os.environ["AGENT_KEY"] = "testkey123"
os.environ["SESSION_SECRET"] = "testsecret"
os.environ["MUSEFM_MODS"] = "ownerhuman"
os.environ.pop("PORT", None)
sys.path.insert(0, REPO)

import app as A  # noqa: E402 (runs init_db -> dm.ensure_dm_schema)
import dm as DM  # noqa: E402

db = A.db
fails = []

def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" — " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)

TK = "agent:a|human:b"
VIEW = "owner:ownerhuman"

# --- 1. same-second race ---
t0 = int(time.time())
db.dm_send(TK, "agent:a", "human:b", "first", now=t0)
db.dm_mark_seen(TK, VIEW, now=t0)          # look happens
db.dm_send(TK, "agent:a", "human:b", "same-second", now=t0)  # lands same second
c = db.dm_unseen_counts(VIEW, [TK]).get(TK, 0)
check("same-second message still counts as unseen", c == 1, c)

db.dm_mark_seen(TK, VIEW, now=t0)
c = db.dm_unseen_counts(VIEW, [TK]).get(TK, 0)
check("re-look clears it", c == 0, c)

# later message relights
db.dm_send(TK, "agent:a", "human:b", "later", now=t0 + 5)
c = db.dm_unseen_counts(VIEW, [TK]).get(TK, 0)
check("later message relights badge", c == 1, c)

# --- 2. migration from old schema ---
TK2 = "agent:c|human:d"
t1 = int(time.time())
m1 = db.dm_send(TK2, "agent:c", "human:d", "old one", now=t1 - 100)
m2 = db.dm_send(TK2, "agent:c", "human:d", "old two", now=t1 - 50)
# simulate legacy DB: drop column
db.db.execute("ALTER TABLE dm_seen DROP COLUMN seen_msg_id")
db.db.execute(
    "INSERT INTO dm_seen (thread_key, viewer, seen_at) VALUES (?,?,?)",
    (TK2, VIEW, t1 - 60))  # saw first message only
db.db.commit()
DM.ensure_dm_schema(db)  # migration runs
row = db._q("SELECT seen_msg_id FROM dm_seen WHERE thread_key=? AND viewer=?",
            (TK2, VIEW))
check("backfill sets exact seen_msg_id", row and int(row[0]["seen_msg_id"]) == m1,
      row[0]["seen_msg_id"] if row else None)
c = db.dm_unseen_counts(VIEW, [TK2]).get(TK2, 0)
check("backfill preserves one unread", c == 1, c)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
