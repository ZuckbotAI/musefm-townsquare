#!/usr/bin/env python3
"""
Proving test for the NEW P2 found in the 2026-09-22 12:35 tester loop
(adversarial QA): three public GET endpoints return a raw CPython exception
string in their 400 body for a non-integer `project_id` query param:

  GET /api/workroom/tasks?project_id=abc  -> 400
      {"error":"invalid literal for int() with base 10: 'abc'","ok":false}
  GET /api/swarm/snapshot?project_id=abc  -> same
  GET /api/swarm/journal?project_id=abc   -> same

Expected: a clean message like "bad project_id: must be an integer"
(the codebase's own _int_field convention, docstring: "never a raw Python
exception string"). Raw trace internals in API errors are an info-leak /
consistency wart, not a crash (still 400, zero tracebacks in server.log).

Tests only — no app source changes. Expected to FAIL on the current checkout;
documents the exact repro and the expected behavior.

Run:  python3 test_testerloop_rawint_exn_2026_09_22.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-townsquare-tl221235-rawint.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


ENDPOINTS = [
    "/api/workroom/tasks",
    "/api/swarm/snapshot",
    "/api/swarm/journal",
]


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    for ep in ENDPOINTS:
        r = c.get(ep, query_string={"project_id": "abc"})
        d = r.get_json() or {}
        err = str(d.get("error", ""))
        check(f"{ep} still 400s on project_id=abc",
              r.status_code == 400, f"got {r.status_code}")
        check(f"{ep} error is not a raw Python exception string",
              "invalid literal" not in err,
              f"error={err!r}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
