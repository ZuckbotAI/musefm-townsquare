#!/usr/bin/env python3
"""
Proving test for the NEW P2 found in the 2026-09-22 06:35 tester loop
(agent tester): /api/platform-key omits the {"ok": true, ...} envelope that
/api/docs section 2 ("Conventions") promises for ALL successful responses.

Tests only — no app source changes. Expected to FAIL on the current checkout;
documents the exact repro and the expected behavior.

Run:  python3 test_testerloop_platformkey_envelope_2026_09_22.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-townsquare-tl220635-platformkey.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    r = c.get("/api/platform-key")
    d = r.get_json()
    check("GET /api/platform-key -> 200", r.status_code == 200, r.status_code)
    check("platform-key body carries the documented ok envelope",
          bool(d) and d.get("ok") is True, d)
    # The useful fields must still be there either way.
    check("key_id present", bool(d) and "key_id" in d, d)
    check("public_key present", bool(d) and "public_key" in d, d)

    print(f"\n{PASS.__len__()} passed, {FAIL.__len__()} failed: {FAIL}")
    sys.exit(0 if not FAIL else 1)


main()
