#!/usr/bin/env python3
"""
Proving tests for two NEW P2s found in the 2026-09-23 18:35 tester loop run.

1. (human tester) Signed image upload of REAL PNG bytes named "fake.svg"
   -> 200 accepted, but the DB row's `filename` column records "fake.svg"
   while `mime` is "image/png". The stored file is correctly normalized to
   the magic-byte-detected extension (img-<uid>.png), but the recorded
   original filename keeps the misleading .svg extension. Expected: the
   recorded filename is normalized to the detected bytes (e.g. "fake.png")
   or the upload is rejected.

2. (adversarial) POST /api/forum/vote with a signed envelope that OMITS
   `value` -> 200 and records a real upvote, because
   `value = _int_field(data, "value", 1)` defaults a missing field to 1.
   A malformed request silently becomes an upvote. Expected: 400 when
   `value` is absent.

Tests only -- no app source changes. Expected to FAIL on the current
checkout; each check documents the exact repro and the expected behavior.

Run:  python3 test_testerloop_1835_p2s_2026_09_23.py
"""
import base64
import hashlib
import io
import os
import shutil
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from db import Database, ensure_human_auth_schema
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-tl231835-p2s.db"
TEST_DATA = "/tmp/test-townsquare-tl231835-p2s-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_png(w=4, h=4):
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(
            ">I", zlib.crc32(c) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x89PNG" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    import ai_images
    ai_images.ensure_ai_schema(appmod.db)  # ai_uploads table (mirrors upload path)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    # --- setup: registered identity + seed post ---
    priv = Ed25519PrivateKey.generate()
    priv_b64 = b64u(priv.private_bytes_raw())
    pub_b64 = b64u(priv.public_key().public_bytes_raw())
    r = c.post("/api/identity/register",
               json={"handle": "tl231835", "public_key": pub_b64})
    fm_id = (r.get_json() or {}).get("fm_id")
    check("identity register -> 200", r.status_code == 200, r.status_code)
    post_id = appmod.db.create_post("lobby", "tl231835",
                                    "tl231835 seed post", "seed body")
    check("seed post created", bool(post_id), post_id)

    # --- P2 #1: PNG bytes named fake.svg keep the .svg label ---
    png = make_png()
    fields = signed_body(
        priv_b64, "upload", fm_id, title="svg label test",
        file_sha256=hashlib.sha256(png).hexdigest(),
        mime="image/png", ai_generated="1")
    data = dict(fields)
    data["image"] = (io.BytesIO(png), "fake.svg", "image/png")
    r = c.post("/api/upload/image", data=data,
               content_type="multipart/form-data")
    d = r.get_json() or {}
    check("png-as-fake.svg upload accepted (200)", r.status_code == 200,
          f"{r.status_code} {d}")
    row = None
    if r.status_code == 200:
        row = ai_images.get_image_upload(appmod.db, d["id"])
    fname = (row or {}).get("filename", "")
    mime = (row or {}).get("mime", "")
    check("recorded filename matches detected bytes (not .svg)",
          r.status_code != 200 or not fname.lower().endswith(".svg"),
          f"filename={fname!r} mime={mime!r}")

    # --- P2 #2: omitted vote value silently becomes an upvote ---
    env = signed_body(priv_b64, "vote", fm_id,
                      target_type="post", target_id=post_id)
    r = c.post("/api/forum/vote", json=env)
    d = r.get_json() or {}
    check("vote without value -> 400", r.status_code == 400,
          f"{r.status_code} {d}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    sys.exit(0 if not FAIL else 1)


main()
