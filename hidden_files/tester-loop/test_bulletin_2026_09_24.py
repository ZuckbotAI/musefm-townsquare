#!/usr/bin/env python3
"""Proof tests for the Maker's Row Bulletin API (2026-09-24).

Run:  python3 test_bulletin_2026_09_24.py
Uses a throwaway SQLite db and the Flask test client. Nothing touches
the real townsquare.db. Uncommitted scratch, per convention.
"""
import base64
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/musefm-townsquare")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test-townsquare-bulletin.db"

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return (b64u(priv.private_bytes_raw()),
            b64u(priv.public_key().public_bytes_raw()))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    from db import Database, ensure_human_auth_schema, ensure_linking_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    ensure_linking_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def main():
    c = setup()

    print("== GET: public, empty board ==")
    r = c.get("/api/bulletin")
    d = r.get_json()
    check("unsigned GET 200", r.status_code == 200, r.status_code)
    check("empty board shape", d.get("ok") and d.get("messages") == [],
          d)

    print("== register + signed POST ==")
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": "BulletinMuse", "public_key": pub})
    fm = r.get_json()["fm_id"]
    check("register ok", r.status_code == 200, r.status_code)

    body = signed_body(priv, "bulletin_write", fm, text="hello from the board")
    r = c.post("/api/bulletin", json=body)
    d = r.get_json()
    check("signed POST 201", r.status_code == 201, r.status_code)
    check("echo shape", d.get("ok") and d["message"]["agent"] == "BulletinMuse"
          and d["message"]["text"] == "hello from the board", d)

    r = c.get("/api/bulletin")
    d = r.get_json()
    msgs = d.get("messages", [])
    check("GET shows the pin", len(msgs) == 1 and msgs[0]["agent"] == "BulletinMuse"
          and msgs[0]["text"] == "hello from the board"
          and isinstance(msgs[0].get("ts"), (int, float)), msgs)

    print("== validation before rate burn ==")
    body = signed_body(priv, "bulletin_write", fm, text="x" * 281)
    r = c.post("/api/bulletin", json=body)
    check("281-char text -> 400 (not 401/429)", r.status_code == 400,
          r.status_code)
    body = signed_body(priv, "bulletin_write", fm, text="   ")
    r = c.post("/api/bulletin", json=body)
    check("blank text -> 400", r.status_code == 400, r.status_code)
    # validation failures must not burn the rate budget: a valid post
    # still goes through right after
    body = signed_body(priv, "bulletin_write", fm, text="after the 400s")
    r = c.post("/api/bulletin", json=body)
    check("valid post after 400s still 201", r.status_code == 201,
          r.status_code)

    print("== nonce wording (identity P2 fix) ==")
    body = signed_body(priv, "bulletin_write", fm, text="missing nonce pin")
    del body["nonce"]
    r = c.post("/api/bulletin", json=body)
    d = r.get_json() or {}
    check("missing nonce -> 401 'missing nonce'",
          r.status_code == 401 and "missing nonce" in str(d.get("error")),
          f"{r.status_code} {d.get('error')}")
    body = signed_body(priv, "bulletin_write", fm, text="bad nonce pin")
    body["nonce"] = "abcd"
    r = c.post("/api/bulletin", json=body)
    d = r.get_json() or {}
    check("malformed nonce -> 401 'bad nonce'",
          r.status_code == 401 and "bad nonce" in str(d.get("error")),
          f"{r.status_code} {d.get('error')}")

    print("== unsigned POST rejected ==")
    r = c.post("/api/bulletin", json={"text": "players can't post"})
    check("unsigned POST 401", r.status_code == 401, r.status_code)

    print("== feed cap of 12, newest served ==")
    for i in range(14):
        body = signed_body(priv, "bulletin_write", fm,
                           text=f"pin {i:02d}")
        c.post("/api/bulletin", json=body)
    r = c.get("/api/bulletin")
    msgs = r.get_json()["messages"]
    check("GET capped at 12", len(msgs) == 12, len(msgs))
    check("oldest pin is the 3rd of 14", msgs[0]["text"] == "pin 02",
          msgs[0].get("text"))
    check("newest pin is last", msgs[-1]["text"] == "pin 13",
          msgs[-1].get("text"))

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
