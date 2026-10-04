#!/usr/bin/env python3
"""Proving tests for the /vote rate-bucket P2 (filed 2026-09-26-1835):

`/vote` called check_limit("vote", 120) BEFORE auth, so anonymous
hammering burned the shared per-IP vote bucket: after 120 anonymous
POSTs, a signed-in user's first real vote 429'd.

Expected: anonymous requests record nothing against the bucket. 120
anonymous POSTs leave the bucket untouched, and the signed-in vote
succeeds. (A genuinely exhausted bucket still 429s, via the peek.)

Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
Run:  python3 test_testerloop_vote_anon_bucket_2026_09_27.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

TEST_DB = "/tmp/test-townsquare-vote-bucket.db"
TEST_DATA = "/tmp/test-townsquare-vote-bucket-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    for p in (TEST_DB, TEST_DATA):
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)
    import app as appmod

    appmod.db = appmod.init_db(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    os.makedirs(TEST_DATA, exist_ok=True)
    appmod.app.config["TESTING"] = True
    db = appmod.db

    db.register_identity("votehammer", "k" * 43)
    fm = db._one("SELECT fm_id FROM identities WHERE handle='votehammer'"
                 )["fm_id"]

    from app import _csrf_token
    with appmod.app.test_request_context():
        tok = _csrf_token()

    # Anonymous client: hammer /vote 120 times (the full bucket).
    anon = appmod.app.test_client()
    r = anon.post("/submit", data={"x": "1"})  # no-op, keeps client warm
    # Need a real post to vote on; create it via a signed-in client first.
    poster = appmod.app.test_client()
    with poster.session_transaction() as s:
        s["fm_id"] = fm
        s["session_epoch"] = (appmod.db.get_identity(fm) or {}).get("session_epoch", 0)  # P1 2026-10-03: cookie epoch must match identities.session_epoch
        s["csrf_token"] = tok
    r = poster.post("/submit", data={"community": "lobby", "title": "vote test",
                                     "body": "vote target post",
                                     "flair": "discussion",
                                     "csrf_token": tok})
    check("poster posts", r.status_code == 302, r.status_code)
    pid = db._one("SELECT id FROM posts WHERE handle='votehammer'"
                  " ORDER BY id DESC")["id"]

    anon_codes = set()
    for _ in range(120):
        rr = anon.post("/vote", json={"target_type": "post",
                                      "target_id": pid, "value": 1})
        anon_codes.add(rr.status_code)
    check("anonymous hammering gets 401s, not 429s",
          anon_codes == {401}, str(sorted(anon_codes)))

    # The signed-in user's first real vote must NOT be rate-limited.
    r = poster.post("/vote", json={"target_type": "post", "target_id": pid,
                                   "value": 1, "csrf_token": tok})
    check("signed-in vote after 120 anon hits -> 200, not 429",
          r.status_code == 200, r.status_code)

    # And a genuinely exhausted bucket still 429s: burn it with real votes.
    # (120/hr bucket; the one real vote above already recorded 1 hit, so
    # hits 2..120 fill it and hit 121 must 429.)
    codes = set()
    for _ in range(119):
        rr = poster.post("/vote", json={"target_type": "post",
                                        "target_id": pid, "value": 1,
                                        "csrf_token": tok})
        codes.add(rr.status_code)
    rlast = poster.post("/vote", json={"target_type": "post",
                                       "target_id": pid, "value": 1,
                                       "csrf_token": tok})
    check("bucket exhaustion still 429s signed-in votes",
          rlast.status_code == 429, str(rlast.status_code))

    print("\n%d passed, %d failed: %s" % (len(PASS), len(FAIL), FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
