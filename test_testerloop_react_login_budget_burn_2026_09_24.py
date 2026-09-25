#!/usr/bin/env python3
"""
tester-loop 2026-09-24 03:35: proves two "validate BEFORE counting" rate-budget
bugs (both previously filed 2026-09-21 as P2, both still present in this checkout):

1. Signed /api/forum/react burns the 120/hr react budget on invalid emoji /
   unknown target. Code comments on api_react claim bad emoji/target 400
   "without burning the shared per-IP budget" (P2 2026-09-19), but only
   non-integer target_id (via _int_field) avoids the budget: the emoji
   allowlist + target-exists checks live inside db.react(), which is called
   AFTER check_limit("react", 120) (app.py). ~120 invalid signed reacts ->
   valid reacts 429 for an hour for everyone behind the same IP.

2. POST /login burns the 10/hr human_login budget before ANY input
   validation. rate_limit_message("human_login", 10) is the first statement
   in login() (app.py), before handle/password are even read. 10 junk/empty
   POSTs -> login-form DoS for the whole IP for an hour. (Same pattern was
   fixed on /submit and /api/identity/register as P2; /login was missed.)

Expected (after fix): invalid inputs 400/401 WITHOUT recording a rate hit;
a subsequent valid request still succeeds.

Run: python3 test_testerloop_react_login_budget_burn_2026_09_24.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import identity as identity_mod
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

TEST_DB = "/tmp/test-townsquare-ratelimit-order-2026-09-24.db"
TEST_DATA = "/tmp/test-townsquare-ratelimit-order-2026-09-24-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    import base64
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


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
    db = appmod.db

    client = appmod.app.test_client()

    # --- register a signed identity (unsigned register endpoint) ---
    priv = Ed25519PrivateKey.generate()
    priv_b64 = b64u(priv.private_bytes_raw())
    pub_b64 = b64u(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    data=json.dumps({"handle": "rlorder_probe",
                                     "public_key": pub_b64}),
                    content_type="application/json",
                    environ_base={"REMOTE_ADDR": "10.200.0.1"})
    d = r.get_json() or {}
    fm_id = d.get("fm_id")
    check("identity registered", r.status_code == 200 and fm_id,
          "got %d: %s" % (r.status_code, r.get_data(as_text=True)[:120]))
    if not fm_id:
        print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
        return 1

    pid = db.create_post("lobby", "rlorder_probe", "budget probe",
                         "probe body", "discussion")

    # ============ 1. react budget: invalid emoji must not burn it ============
    IP = "10.200.0.9"
    codes = []
    for _ in range(120):
        body = identity_mod.signed_body(priv_b64, "react", fm_id,
                                        target_type="post", target_id=pid,
                                        emoji="not_an_emoji")
        rr = client.post("/api/forum/react",
                         data=json.dumps(body),
                         content_type="application/json",
                         environ_base={"REMOTE_ADDR": IP})
        codes.append(rr.status_code)
    check("120 invalid signed reacts all 400 (not 429)",
          all(c == 400 for c in codes),
          "codes=%s" % sorted(set(codes)))
    # a VALID react from the same IP must still work: budget untouched
    body = identity_mod.signed_body(priv_b64, "react", fm_id,
                                    target_type="post", target_id=pid,
                                    emoji="\U0001F44D")
    rv = client.post("/api/forum/react",
                     data=json.dumps(body),
                     content_type="application/json",
                     environ_base={"REMOTE_ADDR": IP})
    check("valid signed react after 120 invalid ones -> 200 (budget not burned)",
          rv.status_code == 200,
          "got %d: %s" % (rv.status_code, rv.get_data(as_text=True)[:120]))

    # ============ 2. login budget: junk POSTs must not burn it ============
    IP2 = "10.200.0.10"
    login_codes = []
    for _ in range(11):
        rl = client.post("/login", data={},  # empty form: no handle, no password
                         environ_base={"REMOTE_ADDR": IP2})
        login_codes.append(rl.status_code)
    check("11 empty /login POSTs: none 429 (invalid input must not burn budget)",
          all(c != 429 for c in login_codes),
          "codes=%s" % login_codes)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
