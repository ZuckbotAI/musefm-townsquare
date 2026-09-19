#!/usr/bin/env python3
"""Bug-fix proof 2026-09-19 (bug 11): concurrent SQLite writes don't 500.

P1 (tester loop 15:35 run): concurrent writes 500'd with
"database is locked".

Root cause: db.award() -> pets.signal_multiplier() -> _pet_full() ->
ensure_pet_schema() ran CREATE TABLE / ALTER TABLE (DDL, which needs an
EXCLUSIVE SQLite lock) on EVERY Signal award. Under concurrency the
EXCLUSIVE requests convoy-collapsed all writers: 16 threads x 25 iters
of create_post + note_nonce + award + flag_post lost ~195/400 writes
with 213 "database is locked" errors (bisect: every combo containing
award() locked; no_award => 0 locks).

Fixes (db.py, pets.py):
  1. ensure_pet_schema(): once per Database instance (was: per pet call).
  2. Database._exec(): BEGIN IMMEDIATE takes the write lock up front;
     only locked/busy OperationalErrors retry (bounded, exponential
     backoff); a statement is never executed twice.
  3. note_nonce(): INSERT first, expired-nonce DELETE best-effort.

This test hammers the same mix and asserts zero lock errors, zero lost
writes, correct flag state, and intact nonce replay semantics.

Run: python3 test_bugfix_sqlite_contention_2026_09_19.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import Database, ensure_forum_flags_schema  # noqa: E402

TEST_DB = "/tmp/test-townsquare-bugfix-contention.db"

PASS, FAIL = [], []
lock = threading.Lock()


def check(name, cond, detail=""):
    with lock:
        (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    db = Database(TEST_DB)
    ensure_forum_flags_schema(db)  # mirrors app.py startup

    n_posts_before = db._one("SELECT COUNT(*) c FROM posts")["c"]
    errors, locked = [], []

    def worker(wid):
        for i in range(25):
            try:
                pid = db.create_post("lobby", "w%d" % wid,
                                     "t%d-%d" % (wid, i), "body")
                assert db.note_nonce("nonce-%d-%d" % (wid, i)) is True
                db.award("fm-x", "w%d" % wid, 1, "thread", "post", str(pid))
                db.flag_post("post", pid, "fm-flag-%d" % wid,
                             "flagger%d" % wid, "spam")
            except Exception as e:  # noqa: BLE001 — counting failures
                with lock:
                    errors.append(repr(e))
                    if "locked" in repr(e) or "busy" in repr(e):
                        locked.append(repr(e))

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("no 'database is locked'/'busy' errors", not locked,
          "%d lock errors, e.g. %s" % (len(locked), locked[:1]))
    check("no uncaught write errors at all", not errors,
          "%d errors, e.g. %s" % (len(errors), errors[:2]))

    n_posts = db._one("SELECT COUNT(*) c FROM posts")["c"]
    check("zero lost posts", n_posts - n_posts_before == 400,
          "got %d new, want 400" % (n_posts - n_posts_before))
    n_nonces = db._one("SELECT COUNT(*) c FROM seen_nonces")["c"]
    check("zero lost nonces", n_nonces == 400, "got %d" % n_nonces)
    n_flags = db._one("SELECT COUNT(*) c FROM post_flags")["c"]
    check("all flags recorded", n_flags == 400, "got %d" % n_flags)

    # nonce semantics intact: replay detected, expiry cleanup still runs
    check("nonce replay still detected",
          db.note_nonce("nonce-0-0") is False)
    db._exec("UPDATE seen_nonces SET expires_at = 1")
    check("best-effort expiry cleanup still works",
          db.note_nonce("nonce-cleanup-probe") is True
          and db._one("SELECT COUNT(*) c FROM seen_nonces")["c"] == 1)

    # non-contention SQL errors must NOT be retried/masked
    try:
        db._exec("INSERT INTO no_such_table_xyz VALUES (1)")
        check("bad SQL still raises", False, "no error raised")
    except Exception as e:
        check("bad SQL still raises", "no_such_table_xyz" in str(e), repr(e))

    print("== %d PASS, %d FAIL ==" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
