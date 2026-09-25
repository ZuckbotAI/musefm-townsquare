#!/usr/bin/env python3
"""
Proving tests for the NEW P2 found in the 2026-09-21 21:35 tester loop:
POST /signup and POST /login are CSRF-exempt.

Tests only — no app source changes. All three are expected to FAIL on the
current checkout (0/3 green); each documents the exact repro and the expected
behavior. Local test DB + Flask TestClient only.

Bug: neither signup() nor login() in app.py calls _check_csrf(), and neither
template renders any CSRF material. POST /signup with no token -> 200 and the
account is created; POST /login with no token -> 302 and the session is live
(verified live against the local instance). Login CSRF lets an attacker force
a victim's browser into an attacker-known account.

Run:  python3 test_testerloop_logincsrf_2026_09_21.py
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-townsquare-tl2135-logincsrf.db"
TEST_DATA = "/tmp/test-townsquare-tl2135-logincsrf-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.DATA_DIR = TEST_DATA
    os.makedirs(TEST_DATA, exist_ok=True)
    appmod.app.config["TESTING"] = True
    appmod._hits.clear()  # hermetic rate-limit state
    return appmod.app.test_client()


def main():
    client = setup()

    # --- P2-1: /signup page carries no CSRF material at all
    html = client.get("/signup").get_data(as_text=True)
    has_token = ('name="csrf_token"' in html or
                 re.search(r'<meta name="csrf-token"', html) is not None)
    check("P2-1: /signup page renders a CSRF token", has_token,
          "page has zero CSRF material (no form input, no meta tag)")

    # --- P2-2: POST /signup with valid fields but no token is accepted
    c2 = appmod.app.test_client()
    handle = "TlNoCsrf" + os.urandom(2).hex()
    r = c2.post("/signup", data={"handle": handle, "password": "supersecret1",
                                "password_confirm": "supersecret1"})
    created = (r.status_code == 200 and
               appmod.db.get_identity_by_handle(handle) is not None)
    check("P2-2: POST /signup without token is rejected (403)",
          r.status_code == 403,
          "got %d and the account was %screated" %
          (r.status_code, "" if created else "not "))

    # --- P2-3: POST /login with valid creds but no token yields a live session
    c3 = appmod.app.test_client()
    r = c3.post("/signup", data={"handle": handle + "x", "password": "supersecret1",
                                "password_confirm": "supersecret1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    c4 = appmod.app.test_client()  # fresh jar, like a cross-site POST
    r = c4.post("/login", data={"handle": handle + "x",
                               "password": "supersecret1"})
    live = (r.status_code == 302 and c4.get("/settings").status_code == 200)
    check("P2-3: POST /login without token is rejected (403)",
          r.status_code == 403,
          "got %d and the forged session is %slive (/settings -> %d)" %
          (r.status_code, "" if live else "not ",
           c4.get("/settings").status_code))

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
