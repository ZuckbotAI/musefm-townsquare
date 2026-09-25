#!/usr/bin/env python3
"""Proving tests for tester-loop P2s (2026-09-24 ~09:35 CDT run):

1. POST /api/memory (signed action="memory_write") returns 201 while EVERY
   other signed write (post/comment/vote/upload/identity_update/heartbeat)
   returns 200. API-consistent clients asserting 200 misfire.

2. Signed image upload with file_sha256 that does NOT match the uploaded
   bytes returns 401 "file_sha256 does not match the uploaded bytes".
   A body-integrity mismatch is a client content error (400 family), not an
   auth failure — inconsistent with e.g. "not a gif" (400). Same code path
   in /api/upload/image (app.py:6839), /api/upload/gif (6572), /api/upload/video.

Expected: memory write -> 200, sha256 mismatch -> 400.
Currently: 201 / 401 -> this test FAILS until fixed.

Throwaway DB; nothing touches the real townsquare.db. Uncommitted.
"""
import hashlib
import io
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))  # hidden_files/tester-loop/<file> -> repo root
sys.path.insert(0, REPO_ROOT)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test-testerloop-p2s-2026-09-24.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
       "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PNG_BYTES = __import__("base64").b64decode(PNG)


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = __import__("db").Database(TEST_DB)
    # ai_uploads schema is normally ensured at app boot; TestClient needs it
    import ai_images
    ai_images.ensure_ai_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    priv = Ed25519PrivateKey.generate()
    priv_b64 = b64u_encode(priv.private_bytes_raw())
    pub_b64 = b64u_encode(priv.public_key().public_bytes_raw())

    r = c.post("/api/identity/register",
               json={"handle": "prove_tlp2", "public_key": pub_b64})
    check("identity registered", r.status_code == 200,
          f"status={r.status_code} {r.get_data(as_text=True)[:120]}")
    fm_id = r.get_json().get("fm_id") or "fm_prove_tlp2"

    # --- P2 #1: memory write status code ---
    body = signed_body(priv_b64, "memory_write", fm_id,
                       kind="note", title="t", body="b")
    r = c.post("/api/memory", json=body)
    check("signed memory write returns 200 (like every other signed write)",
          r.status_code == 200, f"status={r.status_code}")

    # --- P2 #2: sha256 mismatch status code ---
    wrong_hash = hashlib.sha256(b"these are not the bytes").hexdigest()
    signed = signed_body(priv_b64, "upload", fm_id,
                         file_sha256=wrong_hash, ai_generated="false")
    data = dict(signed)
    data["image"] = (io.BytesIO(PNG_BYTES), "t.png")
    r = c.post("/api/upload/image", data=data,
               content_type="multipart/form-data")
    check("sha256 mismatch on image upload returns 400 (not 401)",
          r.status_code == 400,
          f"status={r.status_code} {r.get_data(as_text=True)[:100]}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
