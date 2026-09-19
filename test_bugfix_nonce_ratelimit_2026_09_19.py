#!/usr/bin/env python3
"""Bug-fix proof 2026-09-19: 429s no longer burn the one-time nonce.

P2 (tester loop 15:35 run): require_agent_or_signature ran verify_signed_body
(which consumes the nonce) BEFORE the route body's check_limit, so every 429
forced the client to re-sign. The decorator now PEEKS at the rate limit
(rate=(bucket, max, window)) before verifying the signature.

Repro: sign a body for POST /api/forum/post, burn the 5/hr budget, then
POST a 6th distinct signed body -> 429. Resend the IDENTICAL 6th body ->
must be 429 again, NOT 401 "replay: nonce already used".

Run: python3 test_bugfix_nonce_ratelimit_2026_09_19.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import signed_body, b64u_encode

TEST_DB = "/tmp/test-townsquare-nonce-ratelimit.db"


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    priv = Ed25519PrivateKey.generate()
    pub = b64u_encode(priv.public_key().public_bytes_raw())
    r = c.post("/api/identity/register",
               json={"handle": "NonceMuse1", "public_key": pub},
               environ_base={"REMOTE_ADDR": "10.99.0.2"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    fm_id = r.get_json()["fm_id"]
    key = b64u_encode(priv.private_bytes_raw())
    ip = {"REMOTE_ADDR": "10.99.0.1"}

    # burn the 5/hr post budget with 5 distinct signed bodies
    for i in range(5):
        body = signed_body(key, "post", fm_id, community="lobby",
                           title="nonce-t%d" % i, body="b%d" % i)
        r = c.post("/api/forum/post", json=body, environ_base=ip)
        assert r.status_code == 200, (r.status_code,
                                     r.get_data(as_text=True)[:200])
    print("  budget burned: 5x200")

    # 6th distinct body -> 429 (nonce must NOT be consumed by the 429)
    body6 = signed_body(key, "post", fm_id, community="lobby",
                        title="nonce-t6", body="b6")
    r = c.post("/api/forum/post", json=body6, environ_base=ip)
    assert r.status_code == 429, (r.status_code, r.get_data(as_text=True)[:200])
    print("  6th distinct body -> 429")

    # resend the IDENTICAL body: 429 again, never 401 replay
    r2 = c.post("/api/forum/post", json=body6, environ_base=ip)
    txt = r2.get_data(as_text=True)
    print("  identical resend -> %d %s" % (r2.status_code, txt[:120]))
    assert r2.status_code == 429, (r2.status_code, txt[:200])
    assert "nonce already used" not in txt, txt[:200]

    # and the nonce really is still live: use it from a fresh IP (no limit)
    # -> must succeed, proving the 429 never burned it
    r3 = c.post("/api/forum/post", json=body6,
                environ_base={"REMOTE_ADDR": "10.99.0.9"})
    assert r3.status_code == 200, (r3.status_code,
                                  r3.get_data(as_text=True)[:200])
    print("  same body from fresh IP -> 200 (nonce was never burned)")
    print("PASS: 429 does not burn the nonce")


if __name__ == "__main__":
    main()
