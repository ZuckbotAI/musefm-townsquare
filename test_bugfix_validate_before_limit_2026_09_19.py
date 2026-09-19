#!/usr/bin/env python3
"""Bug-fix proof 2026-09-19: malformed requests don't burn the rate budget.

P2 (tester loop 15:35 run): 30 rapid malformed POSTs to
/api/identity/register -> first 6 got 400, rest 429; the 10/hr bucket
counted validation-failed requests, locking legitimate registrations
from the same IP out for a full hour. The route now validates the
request shape BEFORE check_limit.

Repro: 30 malformed POSTs from one IP -> all 400 (zero 429s), then a
VALID registration from the same IP -> 200 (budget intact).

Run: python3 test_bugfix_validate_before_limit_2026_09_19.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import b64u_encode

TEST_DB = "/tmp/test-townsquare-validate-before-limit.db"


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    ip = {"REMOTE_ADDR": "10.98.0.1"}

    malformed = [
        {},
        {"handle": "x"},                       # too short
        {"handle": "Bad Handle!"},              # bad chars
        {"handle": 12345, "public_key": "x"},  # non-string
        {"handle": "okhandle1"},                # missing public_key
        {"handle": "okhandle2", "public_key": "!!!not-b64!!!"},
        {"handle": "okhandle3", "public_key": b64u_encode(b"short"),
         "avatar_url": "ftp://evil.example/x"},
    ]
    statuses = set()
    for i in range(30):
        body = malformed[i % len(malformed)]
        r = c.post("/api/identity/register", json=body, environ_base=ip)
        statuses.add(r.status_code)
        assert r.status_code == 400, (i, r.status_code,
                                     r.get_data(as_text=True)[:160])
    print("  30 malformed POSTs -> all 400, statuses seen: %s" % (statuses,))
    assert 429 not in statuses

    # the budget must be untouched: a valid registration still goes through
    priv = Ed25519PrivateKey.generate()
    pub = b64u_encode(priv.public_key().public_bytes_raw())
    r = c.post("/api/identity/register",
               json={"handle": "LegitMuse9", "public_key": pub},
               environ_base=ip)
    assert r.status_code == 200, (r.status_code,
                                 r.get_data(as_text=True)[:200])
    print("  valid registration from the same IP -> 200 (budget intact)")

    # claim-human got the same treatment
    for i in range(12):
        r = c.post("/api/identity/claim-human", json={"handle": "!!"},
                   environ_base={"REMOTE_ADDR": "10.98.0.2"})
        assert r.status_code == 400, (i, r.status_code,
                                     r.get_data(as_text=True)[:160])
    r = c.post("/api/identity/claim-human", json={"handle": "ClaimHuman9"},
               environ_base={"REMOTE_ADDR": "10.98.0.2"})
    assert r.status_code == 200, (r.status_code,
                                 r.get_data(as_text=True)[:200])
    print("  claim-human: 12 malformed -> 400s, then valid -> 200")
    print("PASS: malformed requests never touch the rate budget")


if __name__ == "__main__":
    main()
