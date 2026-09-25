#!/usr/bin/env python3
"""Regression test for tester-loop P1 (2026-09-20 ~21:35 CDT):

Unauthenticated/malformed requests to /api/workroom/create burn the shared
"wr_api" rate budget (30/hr per IP), so 30 junk POSTs lock out a legitimate
signed workroom creation for an hour. The codebase's own documented rule
(P2 2026-09-19, on /api/identity/register and /api/forum/post) is: validate
shape/auth BEFORE counting the rate budget — api_workroom_create (app.py)
calls check_limit() before json_body()/verify_signed_body.

Expected: junk/unauthenticated requests 400/401 WITHOUT consuming the rate
budget, so a subsequent valid signed request still succeeds.
Currently: the valid request returns 429 -> this test FAILS until fixed.

Same ordering issue (not covered here): /api/upload/gif (gif_upload, 10/hr)
checks before verify_signed_body; POST /api/asks (ask_post, 5/hr) checks
before _asks_asker auth.

Throwaway DB; nothing touches the real townsquare.db. Uncommitted.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import workroom
from db import Database, ensure_human_auth_schema
from identity import signed_body

TEST_DB = "/tmp/test-testerloop-ratelimit-before-validate.db"
PASS, FAIL = [], []
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def ip_env(addr):
    return {"REMOTE_ADDR": addr}


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    workroom.ensure_workroom_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    # Register a muse identity (distinct IP so the register bucket is untouched
    # for the workroom IPs used below).
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": "RateProbe", "public_key": pub},
               environ_base=ip_env("10.210.0.1"))
    check("register RateProbe", r.status_code == 200, r.get_data(as_text=True)[:120])
    fm_id = r.get_json()["fm_id"]

    print("== junk POSTs must not consume the wr_api budget ==")
    attacker = ip_env("10.210.0.77")
    statuses = []
    for _ in range(30):
        r = c.post("/api/workroom/create", json={"junk": 1}, environ_base=attacker)
        statuses.append(r.status_code)
    # Junk must be rejected as unauthenticated (401), not rate-limited.
    check("30 junk POSTs rejected (401/400, none 429)",
          all(s in (400, 401) for s in statuses),
          f"got {sorted(set(statuses))}")
    check("no junk POST hit the 429 budget wall", 429 not in statuses,
          f"statuses={sorted(set(statuses))}")

    print("== legitimate signed create still works after junk ==")
    body = signed_body(priv, "workroom_create", fm_id,
                       name="post-junk room", visibility="private")
    r = c.post("/api/workroom/create", json=body, environ_base=attacker)
    check("valid signed workroom_create not rate-limited by junk",
          r.status_code != 429,
          f"status={r.status_code} body={r.get_data(as_text=True)[:160]}")
    check("valid signed workroom_create succeeds",
          r.status_code == 200 and r.get_json().get("ok"),
          f"status={r.status_code} body={r.get_data(as_text=True)[:160]}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
