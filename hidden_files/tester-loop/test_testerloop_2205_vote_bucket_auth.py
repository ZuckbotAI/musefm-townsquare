#!/usr/bin/env python3
"""Proving test for tester-loop P2 (bugs-2026-10-03-2205.md):

Human-form POST /vote burns the 120/hr IP rate bucket BEFORE checking
auth. Repro: N anonymous POSTs to /vote all return 401 (correct), but each
one records a hit via check_limit(), so after 120 anonymous attempts the
121st ALSO returns 429 instead of 401 - the shared IP budget is gone,
burned by traffic that was never authenticated.

Expected after fix: anonymous /vote attempts return 401 WITHOUT touching
the vote bucket (auth checked before the bucket, like the signed API's
peek-before-burn pattern).

Run: python3 hidden_files/tester-loop/test_testerloop_2205_vote_bucket_auth.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", ".."))

import app as appmod

TEST_DB = "/tmp/test-townsquare-tl2205-vote.db"
TEST_DATA = "/tmp/test-townsquare-tl2205-vote-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          ((" -- " + str(detail)) if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    # wipe the in-memory rate buckets so the run is hermetic
    appmod._hits.clear()
    c = appmod.app.test_client()

    # 120 anonymous vote attempts: all should 401 (not signed in)
    codes = []
    for _ in range(120):
        r = c.post("/vote", json={"target_type": "post",
                                  "target_id": 1, "value": 1})
        codes.append(r.status_code)
    check("120 anonymous /vote attempts all 401",
          all(x == 401 for x in codes), sorted(set(codes)))

    # the 121st anonymous attempt: if the bucket was never touched by
    # unauthenticated traffic, this is still a 401. If anonymous attempts
    # burned the budget, it is a 429 - the bug.
    r = c.post("/vote", json={"target_type": "post",
                              "target_id": 1, "value": 1})
    check("121st anonymous /vote still 401 (bucket untouched by anon)",
          r.status_code == 401, r.status_code)

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES: %s" % FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
