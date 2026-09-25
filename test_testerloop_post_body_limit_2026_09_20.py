#!/usr/bin/env python3
"""
Regression test (failing) for 2026-09-20 00:35 tester loop finding:
POST /api/forum/post (and the web /submit route) accept UNBOUNDED bodies
(adversarial QA proved a 9MB body stored live), while comments are capped
at 2000 chars. Expect: oversized post bodies are rejected (400 / 302-with-error),
NOT stored.

Run:  python3 test_testerloop_post_body_limit_2026_09_20.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from identity import b64u_encode, new_nonce, signed_body
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

TEST_DB = "/tmp/test-townsquare-postbody-20260920.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return b64u_encode(b)


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta"
    return m.group(1)


def main():
    c = setup()
    ip = {"REMOTE_ADDR": "10.77.0.1"}

    # --- signed API: register an agent identity ---
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": "BodyLimitAgent", "public_key": pub})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    fm_id = r.get_json()["fm_id"]

    big = "x" * 9_000_000  # 9MB, as the adversarial tester proved live
    body = signed_body(priv, "post", fm_id, community="lobby",
                       title="big", body=big)
    r = c.post("/api/forum/post", json=body, environ_base=ip)
    d = r.get_json() or {}
    check("signed POST /api/forum/post with 9MB body rejected (400)",
          r.status_code == 400 and not d.get("ok"),
          f"status={r.status_code} ok={d.get('ok')}")

    # sanity: a normal-size body still works
    body2 = signed_body(priv, "post", fm_id, community="lobby",
                        title="small", body="hello world")
    r2 = c.post("/api/forum/post", json=body2, environ_base=ip)
    check("signed POST with small body still accepted",
          r2.status_code == 200 and (r2.get_json() or {}).get("ok"),
          r2.status_code)

    # --- web /submit route: human session ---
    human = appmod.app.test_client()
    r = human.post("/signup", data={"handle": "BodyLimitHuman",
                                    "password": "supersecret1",
                                    "password_confirm": "supersecret1"})
    assert r.status_code == 200, r.get_data(as_text=True)[:160]
    r = human.post("/login", data={"handle": "BodyLimitHuman",
                                   "password": "supersecret1"})
    assert r.status_code == 302, r.get_data(as_text=True)[:160]
    tok = csrf_of(human)
    r = human.post("/submit",
                   data={"community": "lobby", "title": "big",
                         "body": "y" * 200_000, "csrf_token": tok},
                   environ_base={"REMOTE_ADDR": "10.77.0.2"})
    html = r.get_data(as_text=True)
    stored = r.status_code == 302 and "/post/" in (r.headers.get("Location") or "")
    check("POST /submit with 200k-char body NOT stored as a post",
          not stored, f"status={r.status_code} location={r.headers.get('Location')}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
