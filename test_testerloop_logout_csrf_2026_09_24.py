#!/usr/bin/env python3
"""
Proves a P2 (2026-09-24 tester loop, commit 86e3684):
POST /logout has no CSRF check -- a cross-site POST without a csrf_token
kills the victim's session. Every sibling state-changing human route
(/submit, /post/<pid>/comment, /vote, /signals/react, /photos/upload)
403s without a valid token; /logout (app.py logout(): session.clear()
+ redirect, no _check_csrf()) does not.

EXPECTED: POST /logout without csrf_token -> 403 and session SURVIVES.
TODAY:    POST /logout without csrf_token -> 302 and session is dead
          (GET /submit -> 302 /login?next=/submit).

Run:  python3 test_testerloop_logout_csrf_2026_09_24.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-logout-csrf.db"
TEST_DATA = "/tmp/test-townsquare-logout-csrf-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.app.config["TESTING"] = True

    # Control FIRST (own session): a logout WITH a valid csrf token still
    # works -- the future fix must not break legit logout.
    c2 = appmod.app.test_client()
    c2.post("/signup", data={"handle": "logoutcsrf2",
                             "password": "s3cretpw!!",
                             "password_confirm": "s3cretpw!!",
                             "email": "logoutcsrf2@example.com"})
    c2.post("/login", data={"handle": "logoutcsrf2",
                            "password": "s3cretpw!!"})
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">',
                  c2.get("/").get_data(as_text=True))
    if m:
        r = c2.post("/logout", data={"csrf_token": m.group(1)},
                    follow_redirects=False)
        check("logout with valid csrf_token still logs out (302)",
              r.status_code == 302, f"got {r.status_code}")
        r = c2.get("/submit")
        check("after legit logout: /submit 302 to login",
              r.status_code == 302, f"got {r.status_code}")
    else:
        check("control skipped: no csrf meta for logged-in client", False)

    # THE BUG PROOF: victim session, cross-site-style logout, no csrf_token
    c = appmod.app.test_client()
    r = c.post("/signup", data={"handle": "logoutcsrf",
                                "password": "s3cretpw!!",
                                "password_confirm": "s3cretpw!!",
                                "email": "logoutcsrf@example.com"})
    check("signup with email 200", r.status_code == 200,
          f"got {r.status_code}")
    r = c.post("/login", data={"handle": "logoutcsrf",
                               "password": "s3cretpw!!"})
    check("login 200/302", r.status_code in (200, 302),
          f"got {r.status_code}")

    r = c.get("/submit")
    check("logged in: GET /submit 200", r.status_code == 200,
          f"got {r.status_code}")

    r = c.post("/logout", data={}, follow_redirects=False)
    check("POST /logout without csrf_token is REJECTED (403)",
          r.status_code == 403, f"got {r.status_code}")

    r = c.get("/submit")
    check("session survives: GET /submit still 200",
          r.status_code == 200, f"got {r.status_code}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
