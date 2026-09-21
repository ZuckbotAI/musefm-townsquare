#!/usr/bin/env python3
"""
Proving tests for the 3 NEW P2s found in the 2026-09-21 09:35 tester loop.

Tests only — no app source changes. All three are expected to FAIL on the
current checkout (0/3 green); each documents the exact repro and the expected
behavior. Local test DB + Flask TestClient only.

Run:  python3 test_testerloop_p2_2026_09_21_c.py
"""
import hashlib
import io
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import videos
from db import Database, ensure_human_auth_schema
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-tl210935c.db"
TEST_DATA = "/tmp/test-townsquare-tl210935c-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = Database(TEST_DB)
    videos.ensure_video_schema(appmod.db)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    appmod._hits.clear()  # hermetic rate-limit state
    return appmod.app.test_client()


def make_mp4(n=5000):
    return (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
            b"\x00\x00\x00\x08" + b"moov" + bytes(n))


def fresh_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64
    priv = Ed25519PrivateKey.generate()
    b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return (b64u(priv.private_bytes_raw()),
            b64u(priv.public_key().public_bytes_raw()))


def register_agent(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


def login_human(handle="TlHumanC", password="supersecret1"):
    me = appmod.app.test_client()
    r = me.post("/signup", data={"handle": handle, "password": password,
                                 "password_confirm": password})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = me.post("/login", data={"handle": handle, "password": password})
    assert r.status_code == 302, r.get_data(as_text=True)
    return me


def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


def main():
    client = setup()

    # --- NEW P2-1: unsigned /api/link_muse with no code -> 400, should be 401
    r = client.post("/api/link_muse", json={})
    check("P2-A1: /api/link_muse missing code, unsigned -> 401 first",
          r.status_code == 401,
          "got %d %s (param validation ran before the auth gate)"
          % (r.status_code, r.get_data(as_text=True)[:80]))

    # --- NEW P2-2: unsigned /api/episodes/<bad-slug>/clips -> 400, should be 401
    r = client.post("/api/episodes/no-such-slug-xyz/clips", json={})
    check("P2-A2: /api/episodes/<bad-slug>/clips, unsigned -> 401 first",
          r.status_code == 401,
          "got %d %s (param validation ran before the auth gate)"
          % (r.status_code, r.get_data(as_text=True)[:80]))

    # --- NEW P2-3: invalid video-comment body burns the rate budget first
    priv, fm_id = register_agent(client, "TlClipMuse")
    raw = make_mp4()
    fields = signed_body(priv, "upload", fm_id,
                         file_sha256=hashlib.sha256(raw).hexdigest(),
                         ai_generated="1")
    data = dict(fields)
    data["video"] = (io.BytesIO(raw), "clip.mp4", "video/mp4")
    r = client.post("/api/upload/video", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_data(as_text=True)
    uid = r.get_json()["id"]

    me = login_human()
    tok = csrf_of(me)
    before = dict(appmod._hits)  # nothing should be recorded for this ip yet
    r = me.post("/video/%d/comment" % uid,
                data={"csrf_token": tok, "body": ""})  # invalid body -> 400
    check("P2-B: invalid video comment body is rejected",
          r.status_code == 400,
          "got %d (need 400 to prove the budget-burn ordering)"
          % r.status_code)
    ip = "127.0.0.1"
    burned = [(k, len(v)) for k, v in appmod._hits.items()
              if k not in before and k[1] == ip]
    check("P2-B: 400-rejected video comment does not burn the rate budget",
          not burned,
          "buckets recorded hits on the 400 path: %r "
          "(expected: validate before limit, per peek_limited docstring)" % (burned,))

    # --- NEW P2-4: JSON POST to /post/<pid>/comment with valid token -> 403
    # create a thread to comment on via the signed API
    post_fields = signed_body(priv, "post", fm_id,
                              title="p2c thread", body="hello")
    r = client.post("/api/forum/post", json=post_fields)
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["id"]
    r = me.post("/post/%d/comment" % pid,
                json={"csrf_token": tok, "body": "json comment"},
                content_type="application/json")
    check("P2-C: JSON comment with valid token is not 403-misleading",
          r.status_code != 403,
          "got 403 'bad form token' despite a valid token in the JSON body "
          "(_check_csrf only reads request.form; sibling routes read JSON)")

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
