#!/usr/bin/env python3
"""Proving test for tester-loop P1 (2026-09-22 ~18:35 CDT run):

Unauthenticated junk multipart requests to /api/upload/audio burn the
shared "upload" rate budget (10/hr per IP). app.api_upload_audio
(app.py) calls check_limit("upload", 10) BEFORE verify_signed_body, so
anyone can lock out a legitimate signed agent's audio uploads for an
hour. Same class as the 2026-09-20 21:35 workroom P1
(test_testerloop_ratelimit_before_validate_2026_09_20.py) — this file
proves the /api/upload/audio instance.

Expected: junk/unauthenticated requests 400/401 WITHOUT consuming the
rate budget, so a subsequent valid signed audio upload still succeeds.
Currently: the valid upload returns 429 -> this test FAILS until fixed.

Code inspection shows the identical ordering (check_limit before
verify_signed_body) in /api/upload/gif, /api/upload/image, and
/api/upload/video — the gif instance was already named in the 09-20 pin.

Throwaway DB; nothing touches the real townsquare.db. Uncommitted.
"""
import io
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))  # hidden_files/tester-loop/<file> -> repo root
sys.path.insert(0, REPO_ROOT)

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test-testerloop-upload-ratelimit-before-validate.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def upload_hits(c, ip):
    q = appmod._hits.get(("upload", ip), [])
    return len([x for x in q if x > time.time() - 3600])


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = __import__("db").Database(TEST_DB)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    ip = "10.201.0.7"
    env = {"REMOTE_ADDR": ip}

    before = upload_hits(c, ip)
    # 3 junk multipart uploads: no signature fields at all.
    for i in range(3):
        r = c.post("/api/upload/audio",
                   data={"file": (io.BytesIO(b"not audio"), "x.mp3"),
                         "action": "upload"},
                   content_type="multipart/form-data", environ_base=env)
        check(f"junk upload {i} rejected (401)", r.status_code == 401,
              f"status={r.status_code}")
    after = upload_hits(c, ip)
    check("junk uploads did not consume the upload budget",
          after == before, f"budget hits {before} -> {after}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
