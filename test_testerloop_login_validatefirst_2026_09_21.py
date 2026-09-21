#!/usr/bin/env python3
"""Regression test for tester-loop P2 (2026-09-21 ~12:35 CDT, adversarial QA):

POST /login burned the 10/hr `human_login` rate budget BEFORE reading or
validating the fields, so blank/bot POSTs could DoS the login form for
everyone behind one NAT IP. The codebase's own documented rule
(peek_limited docstring, app.py: fixed on the human form routes 2026-09-20)
is validate-first: malformed submissions never touch the bucket.

Expected: 11 blank POSTs -> all 401 "bad handle or password", none 429,
and a subsequent well-formed (wrong-password) login from the same IP is
NOT 429 (budget untouched by the blanks).

Throwaway DB; nothing touches the real townsquare.db. Uncommitted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-testerloop-login-validatefirst.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def ip_env(addr):
    return {"REMOTE_ADDR": addr}


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    ip = ip_env("10.211.0.99")

    print("== blank POSTs must 401 without touching the budget ==")
    statuses = []
    for _ in range(11):
        r = c.post("/login", data={"handle": "", "password": ""},
                   environ_base=ip)
        statuses.append(r.status_code)
    check("11 blank POSTs all 401", all(s == 401 for s in statuses),
          f"got {sorted(set(statuses))}")
    check("no blank POST hit the 429 wall", 429 not in statuses,
          f"statuses={sorted(set(statuses))}")
    body = r.get_data(as_text=True)
    check("blank POST body carries the generic 401 error",
          "bad handle or password" in body, body[:120])

    print("== blank handle, filled password also skips the budget ==")
    r = c.post("/login", data={"handle": "", "password": "x" * 20},
               environ_base=ip)
    check("blank handle -> 401", r.status_code == 401,
          str(r.status_code))

    print("== budget still intact for a well-formed attempt ==")
    r = c.post("/login", data={"handle": "nope", "password": "wrongwrong"},
               environ_base=ip)
    check("well-formed wrong-password login -> 401 (not 429)",
          r.status_code == 401, f"got {r.status_code}")

    print("== brute-force protection still works ==")
    for _ in range(9):
        c.post("/login", data={"handle": "nope", "password": "wrongwrong"},
               environ_base=ip)
    r = c.post("/login", data={"handle": "nope", "password": "wrongwrong"},
               environ_base=ip)
    check("11th well-formed attempt -> 429", r.status_code == 429,
          f"got {r.status_code}")
    check("429 carries Retry-After", "Retry-After" in r.headers)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
