#!/usr/bin/env python3
"""
Proving tests for two NEW P2s found in the 2026-09-22 15:35 tester loop run.

1. (agent tester) Signed envelope with NO `signature` key -> 401
   "musefm-v1 auth failed: bad signature encoding". A missing signature is
   not a *bad* signature: verify_signed_body should report "missing
   signature". Same message-quality class as the missing-timestamp P2
   fixed 2026-09-21.

2. (adversarial) POST /api/forum/react with no `target_id` -> 400
   "unknown target". `_int_field` defaults missing target_id to 0 and the
   lookup miss reports "unknown target"; the web route `fb_react_web` got
   the naming-the-param fix on 2026-09-21, `api_react` did not. Expected:
   the 400 names the missing param (e.g. mentions "target_id").

Tests only -- no app source changes. Expected to FAIL on the current
checkout; each check documents the exact repro and the expected behavior.

Run:  python3 test_testerloop_error_messages_2026_09_22.py
"""
import base64
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-townsquare-tl221535-errmsg.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.AGENT_KEY = "test-agent-key"
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    HEADERS = {"X-Agent-Key": "test-agent-key"}

    # --- setup: a post and a registered identity ---
    r = c.post("/api/identity/register", json={
        "handle": "errmsgtl",
        "public_key": b64u(Ed25519PrivateKey.generate().public_key()
                           .public_bytes_raw()),
    })
    fm_id = (r.get_json() or {}).get("fm_id")
    check("identity register -> 200", r.status_code == 200, r.status_code)
    post_id = appmod.db.create_post("lobby", "errmsgtl",
                                    "errmsg seed post", "seed body")
    check("seed post created", bool(post_id), post_id)

    # --- P2 #1: missing signature should say "missing signature" ---
    envelope = {
        "action": "comment",
        "fm_id": fm_id,
        "timestamp": int(time.time() * 1000),  # ms per verify_signed_body
        "nonce": b64u(os.urandom(16)),
        "post_id": post_id,
        "body": "comment without signature",
        # NOTE: no "signature" key at all
    }
    r = c.post("/api/forum/comment", json=envelope)
    d = r.get_json() or {}
    err = str(d.get("error", ""))
    check("missing-signature envelope -> 401", r.status_code == 401,
          f"{r.status_code} {d}")
    check("missing-signature error says 'missing signature'",
          "missing signature" in err.lower(), err)

    # --- P2 #2: react without target_id should name the param ---
    r = c.post("/api/forum/react", headers=HEADERS, json={
        "handle": "errmsgtl",
        "target_type": "post",
        "emoji": "🔥",
        # NOTE: no "target_id"
    })
    d = r.get_json() or {}
    err = str(d.get("error", ""))
    check("react without target_id -> 400", r.status_code == 400,
          f"{r.status_code} {d}")
    check("react missing-target_id error names 'target_id'",
          "target_id" in err.lower(), err)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    sys.exit(0 if not FAIL else 1)


main()
