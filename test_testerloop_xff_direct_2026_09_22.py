#!/usr/bin/env python3
"""Tester-loop 2026-09-22: P1 X-Forwarded-For rate-limit bypass (direct connections).

Filed 2026-09-21 15:35 loop, re-proven LIVE 2026-09-22 12:35 loop:
`app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)` ran UNCONDITIONALLY, so on
direct-origin connections (no Render edge in front) a client-supplied
X-Forwarded-For header was trusted as the client IP. Rotating the header
gave every request a fresh rate-limit bucket -- rate limits were trivially
evaded. Behind Render's edge this is correct (Render appends the real client
IP, ProxyFix takes the last hop), but direct hits must ignore the header.

This file is tests ONLY (tester-loop fix policy: no app source changes).
Both tests currently FAIL on direct connections; they should PASS once
ProxyFix is gated on actually running behind the trusted proxy.

Run:  .venv/bin/python test_testerloop_xff_direct_2026_09_22.py
Uses a throwaway SQLite db and the Flask test client. Nothing touches
the real townsquare.db. Restores app._hits afterwards.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod

TEST_DB = "/tmp/test-townsquare-xff-direct-20260922.db"

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    from db import Database, ensure_human_auth_schema, ensure_linking_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    ensure_linking_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    # hermetic: snapshot and clear the in-memory rate buckets
    saved_hits = dict(appmod._hits)
    appmod._hits.clear()
    try:
        priv = Ed25519PrivateKey.generate()
        pub = b64u(priv.public_key().public_bytes_raw())

        codes = []
        for i in range(11):
            # valid shape (passes the pre-budget validation), distinct handle
            # each time so db.register_identity succeeds and the request
            # COUNTS against the identity_register bucket.
            r = c.post("/api/identity/register",
                       json={"handle": f"XffProbe{i:02d}", "public_key": pub},
                       headers={"X-Forwarded-For": f"9.9.9.{i}"})
            codes.append(r.status_code)

        check("first 10 registrations accepted (200)",
              codes[:10] == [200] * 10, codes[:10])
        check("11th request 429 despite rotated X-Forwarded-For",
              codes[10] == 429, codes[10])

        # mechanism: every hit must land in ONE bucket keyed by the real
        # connection IP (127.0.0.1), never by a spoofed header value.
        keys = {k for k in appmod._hits if k[0] == "identity_register"}
        spoofed = {k for k in keys if k[1].startswith("9.9.9.")}
        check("no identity_register bucket keyed by a spoofed XFF IP",
              not spoofed, sorted(k[1] for k in spoofed))
        real = [k for k in keys if k[1] == "127.0.0.1"]
        check("all 10 counted hits share the 127.0.0.1 bucket",
              len(real) == 1 and len(appmod._hits[real[0]]) == 10,
              {k: len(v) for k, v in appmod._hits.items()
               if k[0] == "identity_register"})
    finally:
        appmod._hits.clear()
        appmod._hits.update(saved_hits)
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
