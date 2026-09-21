#!/usr/bin/env python3
"""Tests for identity public-key rotation (compromise recovery).

Covers:
  1. db.rotate_identity_key: unknown fm_id -> ValueError; bad key -> ValueError
  2. After rotation the new public key is stored; signatures made with the
     NEW private key verify, signatures made with the OLD private key fail
  3. POST /api/admin/identity/rotate-key without the agent key -> 401
  4. With the agent key: rotation succeeds (200), bad public_key -> 400,
     unknown fm_id -> 400

Run:  .venv/bin/python test_identity_key_rotation.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import IdentityError, signed_body, verify_signed_body

TEST_DB = "/tmp/test-townsquare-keyrotation.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.AGENT_KEY = "test-agent-key-rotation"
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def main():
    c = setup()
    db = appmod.db

    old_priv, old_priv_b64, old_pub_b64 = keypair()
    _, new_priv_b64, new_pub_b64 = keypair()

    # register an identity the normal way
    r = c.post("/api/identity/register", json={
        "handle": "rot_test_muse", "public_key": old_pub_b64})
    check("register identity", r.status_code == 200, r.get_data(as_text=True)[:120])
    fm_id = r.get_json()["fm_id"]

    # --- db-level rotation ---
    try:
        db.rotate_identity_key("fm_nope", new_pub_b64)
        check("rotate unknown fm_id raises", False)
    except ValueError:
        check("rotate unknown fm_id raises", True)
    try:
        db.rotate_identity_key(fm_id, "not-a-key")
        check("rotate bad public_key raises", False)
    except ValueError:
        check("rotate bad public_key raises", True)

    ident = db.rotate_identity_key(fm_id, new_pub_b64)
    check("rotate stores new public_key",
          ident["public_key"] == new_pub_b64, ident["public_key"][:20])

    # new key signs OK, old key no longer verifies
    body_new = signed_body(new_priv_b64, "ping", fm_id, v="1")
    try:
        got = verify_signed_body(body_new, db)
        check("new-key signature verifies", got["fm_id"] == fm_id)
    except IdentityError as e:
        check("new-key signature verifies", False, str(e))
    body_old = signed_body(old_priv_b64, "ping", fm_id, v="1")
    try:
        verify_signed_body(body_old, db)
        check("old-key signature rejected after rotation", False)
    except IdentityError:
        check("old-key signature rejected after rotation", True)

    # --- endpoint: auth gate ---
    r = c.post("/api/admin/identity/rotate-key",
               json={"fm_id": fm_id, "public_key": old_pub_b64})
    check("endpoint without agent key -> 401", r.status_code == 401,
          str(r.status_code))
    r = c.post("/api/admin/identity/rotate-key",
               headers={"X-Agent-Key": "wrong"},
               json={"fm_id": fm_id, "public_key": old_pub_b64})
    check("endpoint with wrong agent key -> 401", r.status_code == 401,
          str(r.status_code))

    # --- endpoint: happy path + validation ---
    r = c.post("/api/admin/identity/rotate-key",
               headers={"X-Agent-Key": "test-agent-key-rotation"},
               json={"fm_id": fm_id, "public_key": old_pub_b64})
    check("endpoint with agent key -> 200", r.status_code == 200,
          r.get_data(as_text=True)[:120])
    check("endpoint rotation persisted",
          db.get_identity(fm_id)["public_key"] == old_pub_b64)
    r = c.post("/api/admin/identity/rotate-key",
               headers={"X-Agent-Key": "test-agent-key-rotation"},
               json={"fm_id": fm_id, "public_key": "junk"})
    check("endpoint bad public_key -> 400", r.status_code == 400,
          str(r.status_code))
    r = c.post("/api/admin/identity/rotate-key",
               headers={"X-Agent-Key": "test-agent-key-rotation"},
               json={"fm_id": "fm_nope", "public_key": new_pub_b64})
    check("endpoint unknown fm_id -> 400", r.status_code == 400,
          str(r.status_code))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
