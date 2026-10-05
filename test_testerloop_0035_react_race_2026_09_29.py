#!/usr/bin/env python3
"""Proving test for the concurrent reaction race (tester loop 2026-09-29 00:35).

Bug: 20 concurrent POSTs to /signals/react on the same target/reactor
produce 500s with `sqlite3.IntegrityError: UNIQUE constraint failed:
signals.target_type, signals.target_id, signals.reactor`
(reproduced 8/20 against the local test instance).

Root cause: signals.react() does SELECT-then-INSERT/UPDATE/DELETE with no
write lock held across the check-and-act, so two threads both see "no row"
and both INSERT. db.vote() does not have this problem: it wraps the whole
read-modify-write in BEGIN IMMEDIATE and uses INSERT OR REPLACE.

This test hammers signals.react() from 20 threads sharing one Database
(the same sharing the threaded dev server does) and requires zero
exceptions. It FAILS until the race is fixed. No app source is touched.

Throwaway SQLite db. Nothing touches townsquare.db.
Run: python3 test_testerloop_0035_react_race_2026_09_29.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DB = "/tmp/test-townsquare-reactrace.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    from db import Database
    import signals
    db = Database(TEST_DB)
    signals.ensure_signals_schema(db)
    db.db.execute(
        "INSERT INTO posts (community, handle, title, body, created_at)"
        " VALUES ('lobby','RaceCase','t','b',%d)" % int(time.time()))
    db.db.commit()
    return db


def worker(db, pid, errs):
    try:
        import signals
        signals.react(db, "post", pid, "fm_race1", "racehandle", "fire")
    except Exception as e:
        errs.append(repr(e))


def main():
    print("== concurrent signals.react: no thread may raise ==")
    db = setup()
    pid = db.db.execute("SELECT id FROM posts").fetchone()["id"]
    errs = []
    threads = [threading.Thread(target=worker, args=(db, pid, errs))
               for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("20 concurrent reacts raise nothing", not errs,
          "%d errors, e.g. %s" % (len(errs), errs[0] if errs else ""))
    rows = db.db.execute(
        "SELECT COUNT(*) c FROM signals WHERE target_type='post'"
        " AND target_id=? AND reactor='fm_race1'", (pid,)).fetchone()["c"]
    check("at most one signal row per (target, reactor)", rows <= 1, rows)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
