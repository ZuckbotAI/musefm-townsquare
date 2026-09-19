#!/usr/bin/env python3
"""Bug-fix proof 2026-09-19: CSRF on /submit, /photos/upload, /upload, /fb_react.

P2 (tester loop 15:35 run): several human write routes had no CSRF check.
The routes below previously accepted forged cross-site form POSTs; they now
require the session-bound synchronizer token (403 without it, 403 on a bad
token) while the real in-app forms carry the token (templates render it).

Repro: log in, POST each route with no token -> 403, with a wrong token ->
403, with the real token -> the route works again (302/200, not 403).

Run: python3 test_bugfix_csrf_2026_09_19.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-csrf-0919.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True

    human = appmod.app.test_client()
    r = human.post("/signup", data={"handle": "CsrfHuman",
                                    "password": "supersecret1",
                                    "password_confirm": "supersecret1"})
    assert r.status_code == 200, r.get_data(as_text=True)[:160]
    r = human.post("/login", data={"handle": "CsrfHuman",
                                   "password": "supersecret1"})
    assert r.status_code == 302, r.get_data(as_text=True)[:160]
    tok = csrf_of(human)
    ip = {"REMOTE_ADDR": "10.66.0.1"}

    # ---- /submit ----
    base = {"community": "lobby", "title": "csrf t", "body": "b"}
    r = human.post("/submit", data=dict(base), environ_base=ip)
    check("POST /submit without token -> 403", r.status_code == 403,
          r.status_code)
    r = human.post("/submit", data=dict(base, csrf_token="wrong"),
                   environ_base=ip)
    check("POST /submit with bad token -> 403", r.status_code == 403,
          r.status_code)
    r = human.post("/submit", data=dict(base, csrf_token=tok),
                   environ_base=ip)
    check("POST /submit with valid token -> 302", r.status_code == 302,
          r.status_code)

    # ---- /photos/upload ----
    def photo(tokval):
        d = {"title": "t", "csrf_token": tokval} if tokval else {"title": "t"}
        d["photo"] = (io.BytesIO(
            bytes.fromhex("89504e470d0a1a0a0000000d49484452"
                          "00000001000000010802000000907753de"
                          "0000000c4944415478da626001000000ffff"
                          "03000006000557bfabd40000000049454e44ae426082")),
            "p.png", "image/png")
        return human.post("/photos/upload", data=d,
                          content_type="multipart/form-data",
                          environ_base={"REMOTE_ADDR": "10.66.0.2"})
    check("POST /photos/upload without token -> 403",
          photo(None).status_code == 403, photo(None).status_code)
    check("POST /photos/upload with bad token -> 403",
          photo("wrong").status_code == 403)
    r = photo(tok)
    check("POST /photos/upload with valid token -> 302",
          r.status_code == 302, r.status_code)

    # ---- /upload ----
    def upload(tokval):
        d = {"title": "t"}
        if tokval:
            d["csrf_token"] = tokval
        d["audio"] = (io.BytesIO(b"RIFF....WAVEfmt "), "a.wav", "audio/wav")
        return human.post("/upload", data=d,
                          content_type="multipart/form-data",
                          environ_base={"REMOTE_ADDR": "10.66.0.3"})
    check("POST /upload without token -> 403",
          upload(None).status_code == 403)
    check("POST /upload with bad token -> 403",
          upload("wrong").status_code == 403)

    # ---- /fb_react (form + JSON) ----
    pid = appmod.db._one("SELECT id FROM posts")["id"]
    form = {"target_type": "post", "target_id": str(pid),
            "reaction": "like", "next": "/"}
    r = human.post("/fb_react", data=dict(form),
                   environ_base={"REMOTE_ADDR": "10.66.0.4"})
    check("POST /fb_react form without token -> 403", r.status_code == 403,
          r.status_code)
    r = human.post("/fb_react", data=dict(form, csrf_token="wrong"),
                   environ_base={"REMOTE_ADDR": "10.66.0.4"})
    check("POST /fb_react form with bad token -> 403", r.status_code == 403,
          r.status_code)
    r = human.post("/fb_react", data=dict(form, csrf_token=tok),
                   environ_base={"REMOTE_ADDR": "10.66.0.4"})
    check("POST /fb_react form with valid token -> 302",
          r.status_code == 302, r.status_code)
    r = human.post("/fb_react",
                   json={"target_type": "post", "target_id": pid,
                         "reaction": "love"},
                   environ_base={"REMOTE_ADDR": "10.66.0.5"})
    check("POST /fb_react JSON without token -> 403", r.status_code == 403,
          r.status_code)
    r = human.post("/fb_react",
                   json={"target_type": "post", "target_id": pid,
                         "reaction": "love", "csrf_token": tok},
                   environ_base={"REMOTE_ADDR": "10.66.0.5"})
    check("POST /fb_react JSON with valid token -> 200",
          r.status_code == 200, r.status_code)

    print("== %d PASS, %d FAIL ==" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
