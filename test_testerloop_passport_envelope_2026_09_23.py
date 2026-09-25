#!/usr/bin/env python3
"""
Proving test for the NEW P2 found in the 2026-09-23 09:35 tester loop
(agent tester): /api/passport/<fm_id> returns a bare
{"key_id","payload","signature"} body with no {"ok": true} envelope,
unlike every other /api/* endpoint and the /api/docs section 2
("Conventions") promise that ALL successful responses carry it.
Same envelope class as the 2026-09-22 platform-key P2 (still on file);
this is the passport variant, not the same endpoint.

Tests only — no app source changes. Expected to FAIL on the current
checkout; documents the exact repro and the expected behavior.

Run:  python3 test_testerloop_passport_envelope_2026_09_23.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import identity as idmod
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-townsquare-tl230935-passport.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)  # full schema + seeds, mirrors app startup
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    from nacl.signing import SigningKey
    import base64
    sk = SigningKey.generate()
    pub = base64.b64encode(sk.verify_key.encode()).decode()
    r = c.post("/api/identity/register",
               data=json.dumps({"handle": "tl_passport",
                                "public_key": pub}),
               content_type="application/json")
    d = r.get_json() or {}
    check("identity register -> 200", r.status_code == 200, r.status_code)
    fm_id = d.get("fm_id")
    check("fm_id issued", bool(fm_id), d)

    r = c.get(f"/api/passport/{fm_id}")
    p = r.get_json() or {}
    check("GET /api/passport/<fm_id> -> 200", r.status_code == 200,
          r.status_code)
    check("passport body carries the documented ok envelope",
          p.get("ok") is True, p)
    # The useful fields must still be there either way.
    check("key_id present", "key_id" in p, p)
    check("payload present", "payload" in p, p)
    check("signature present", "signature" in p, p)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    sys.exit(0 if not FAIL else 1)


if __name__ == "__main__":
    main()
