#!/usr/bin/env python3
"""Bug-fix proof 2026-09-20: invalid HTML form POSTs must not burn the
rate-limit budget.

P2 (tester loop 03:35 run, human persona): the 2026-09-19
validate-before-limit fix covered the signed API paths
(/api/identity/register etc.), but the human HTML form routes still call
rate_limit_message() (which RECORDS the hit via limited()) BEFORE any
validation runs. Demonstrated live: 1 valid + 4 invalid (password
mismatch -> 400, no account created) signups from one IP exhausted the
5/hr human_signup bucket, and the next VALID signup 429'd. Same pattern
in submit() [rate check before body-length/title validation] and
add_comment() [rate check before db.create_comment validation].

Expected behavior (matching the signed-API fix): validation failures
(400s) leave the budget untouched, so a valid request from the same IP
still succeeds.

Repro: 5 invalid signups from one IP -> all 400; then a VALID signup
from the same IP -> 200 (currently 429: BUG). Same shape for /submit:
invalid (overlong body -> 400) posts don't count against the 5/hr post
budget, so a later valid post still 302s (currently 429s: BUG).

Run: python3 test_bugfix_form_ratelimit_burn_2026_09_20.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-form-ratelimit-burn-0920.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta on /"
    return m.group(1)


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True

    # ---- /signup: 5 invalid attempts must not lock out a valid one ----
    c = appmod.app.test_client()
    ip = {"REMOTE_ADDR": "10.99.0.1"}
    bad_statuses = set()
    for i in range(5):
        r = c.post("/signup", data={
            "handle": f"burn{i}", "password": "supersecret1",
            "password_confirm": "MISMATCH", "display_name": "x",
            "bio": "x"}, environ_base=ip)
        bad_statuses.add(r.status_code)
    check("invalid signups all 400 (no account created)",
          bad_statuses == {400}, f"got {sorted(bad_statuses)}")
    r = c.post("/signup", data={
        "handle": "burnvalid", "password": "supersecret1",
        "password_confirm": "supersecret1", "display_name": "x",
        "bio": "x"}, environ_base=ip)
    check("valid signup after 5 invalid ones is NOT rate-limited",
          r.status_code == 200, f"got {r.status_code}")

    # ---- /submit: invalid posts must not burn the 5/hr post budget ----
    c2 = appmod.app.test_client()
    ip2 = {"REMOTE_ADDR": "10.99.0.2"}
    r = c2.post("/signup", data={
        "handle": "poster1", "password": "supersecret1",
        "password_confirm": "supersecret1"}, environ_base=ip2)
    assert r.status_code == 200, r.get_data(as_text=True)[:160]
    r = c2.post("/login", data={"handle": "poster1",
                                "password": "supersecret1"},
                environ_base=ip2)
    assert r.status_code == 302, r.get_data(as_text=True)[:160]
    tok = csrf_of(c2)
    bad = set()
    for _ in range(5):
        r = c2.post("/submit", data={
            "csrf_token": tok, "community": "lobby",
            "title": "t", "body": "x" * 10001}, environ_base=ip2)
        bad.add(r.status_code)
    check("overlong bodies all 400 (nothing stored)",
          bad == {400}, f"got {sorted(bad)}")
    r = c2.post("/submit", data={
        "csrf_token": tok, "community": "lobby",
        "title": "real post", "body": "hello"}, environ_base=ip2)
    check("valid post after 5 overlong ones is NOT rate-limited",
          r.status_code == 302, f"got {r.status_code}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
