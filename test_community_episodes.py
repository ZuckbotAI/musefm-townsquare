#!/usr/bin/env python3
"""Module-level tests for community_episodes.py.

Throwaway sqlite + a fake Database (only .db and .get_upload are used by
the module). Nothing touches townsquare.db.

Run:  python3 test_community_episodes.py
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import community_episodes as ce

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


class FakeDB:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self._uploads = {}

    def add_upload(self, uid, fm_id, duration_sec=60):
        self._uploads[uid] = {"id": uid, "fm_id": fm_id,
                              "duration_sec": duration_sec,
                              "stored_path": "uploads/x.mp3"}

    def get_upload(self, uid):
        return self._uploads.get(uid)


def fresh():
    return FakeDB()


# 1. schema is additive + idempotent
db = fresh()
ce.ensure_community_episodes_schema(db)
ce.ensure_community_episodes_schema(db)
check("schema idempotent", True)

# 2. publish happy path
db.add_upload(1, "fm_aaa", 120)
eid = ce.publish_episode(db, "fm_aaa", "tester", 1, "My show, ep 1", "hello")
check("publish returns id", eid == 1, f"got {eid}")
eps = ce.list_episodes(db)
check("listed", len(eps) == 1 and eps[0]["title"] == "My show, ep 1"
      and eps[0]["handle"] == "tester" and eps[0]["duration_sec"] == 120,
      str(eps))
one = ce.get_episode(db, eid)
check("get_episode", one and one["id"] == eid)

# 3. validation failures
def expect_fail(name, fn):
    try:
        fn()
        check(name, False, "no ValueError raised")
    except ValueError:
        check(name, True)
    except Exception as e:
        check(name, False, f"wrong exception: {e!r}")

expect_fail("empty title", lambda: ce.publish_episode(db, "fm_aaa", "t", 1, "  ", "x"))
expect_fail("title too long", lambda: ce.publish_episode(db, "fm_aaa", "t", 1, "x" * 141, "x"))
expect_fail("desc too long", lambda: ce.publish_episode(db, "fm_aaa", "t", 1, "ok", "x" * 2001))
expect_fail("missing upload", lambda: ce.publish_episode(db, "fm_aaa", "t", 999, "ok", "x"))
db.add_upload(2, "fm_other", 60)
expect_fail("foreign upload rejected",
            lambda: ce.publish_episode(db, "fm_aaa", "t", 2, "ok", "x"))
expect_fail("no identity", lambda: ce.publish_episode(db, "", "t", 1, "ok", "x"))

# 4. 10/day cap
db2 = fresh()
for i in range(10):
    db2.add_upload(100 + i, "fm_cap", 30)
    ce.publish_episode(db2, "fm_cap", "capper", 100 + i, f"ep {i}", "")
db2.add_upload(200, "fm_cap", 30)
expect_fail("11th in 24h rejected",
            lambda: ce.publish_episode(db2, "fm_cap", "capper", 200, "ep 10", ""))
check("list shows 10", len(ce.list_episodes(db2)) == 10)

# 5. hide backstop
ce.hide_episode(db2, 1)
check("hidden drops from list", len(ce.list_episodes(db2)) == 9)
mine = ce.my_episodes(db2, "fm_cap")
hidden = [m for m in mine if m["id"] == 1]
check("mine includes hidden",
      len(mine) == 10 and len(hidden) == 1 and hidden[0]["status"] == "hidden")
expect_fail("hide missing id", lambda: ce.hide_episode(db2, 4242))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
