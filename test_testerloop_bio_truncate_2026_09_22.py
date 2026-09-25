#!/usr/bin/env python3
"""
tester-loop 2026-09-22 19:35: proves the signup-bio silent-truncation P2
is FIXED (bugs-2026-09-22-1835.md, finding #4 from human tester).

Bug (before fix): POST /signup or /api/identity/register with a 200KB bio
returned 200 and the DB stored exactly 500 chars — the tail silently lost,
with no error shown. db.register_identity / db.update_identity called
clean(bio, MAX_BIO), which truncates with no signal. Same silent-data-loss
class as the comment-truncation P1.

Fixed behavior: oversize bios are REJECTED with a clear 400
("bio too long — max 500 characters") on all three paths (API register,
signed identity_update, web /signup), and the stored bio is left
untouched. Exactly-500-char bios still go through. db.clean_bio()
replaces the truncating clean(bio, MAX_BIO) on both db paths.

Also pins the already-fixed NUL-strip (P2 2026-09-21, in clean() since
commit a3665c4): control chars are stripped, never stored.

Run: python3 test_testerloop_bio_truncate_2026_09_22.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod

TEST_DB = "/tmp/test-townsquare-bio-truncate-2026-09-22.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.202.0.%d" % _ip[0]}


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    from db import Database, ensure_human_auth_schema, clean_bio, clean_comment_body, MAX_BIO
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.app.config["TESTING"] = True
    db = appmod.db
    client = appmod.app.test_client()

    # --- unit: clean_bio rejects oversize, accepts at-cap, strips controls
    try:
        clean_bio("x" * (MAX_BIO + 1))
        check("clean_bio rejects 501-char bio", False, "no ValueError raised")
    except ValueError as e:
        check("clean_bio rejects 501-char bio", "bio too long" in str(e), str(e))
    check("clean_bio accepts exactly-500-char bio",
          clean_bio("y" * MAX_BIO) == "y" * MAX_BIO)
    check("clean_bio strips NUL/control chars",
          clean_bio("a\x00b\x07c") == "abc")
    check("clean_comment_body strips NUL (P2 2026-09-21 pin)",
          clean_comment_body("a\x00b") == "ab")
    check("clean_comment_body accepts exactly-2000-char body",
          len(clean_comment_body("z" * 2000)) == 2000)

    # --- API register: oversize bio -> 400, nothing created
    _, pub1 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": "BioTrunc1", "public_key": pub1,
                          "bio": "q" * 600},
                    environ_base=fresh_ip())
    check("api register 600-char bio -> 400",
          r.status_code == 400, f"got {r.status_code}")
    check("api register error names the bio cap",
          "bio too long" in r.get_data(as_text=True),
          r.get_data(as_text=True)[:80])

    # handle must NOT be taken afterwards (nothing was created)
    _, pub1b = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": "BioTrunc1", "public_key": pub1b,
                          "bio": "short bio"},
                    environ_base=fresh_ip())
    check("same handle registers fine after the 400 (nothing stored)",
          r.status_code == 200, f"got {r.status_code}")

    # --- API register: exactly-500-char bio -> 200, stored intact
    _, pub2 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": "BioTrunc2", "public_key": pub2,
                          "bio": "w" * 500},
                    environ_base=fresh_ip())
    check("api register 500-char bio -> 200", r.status_code == 200,
          f"got {r.status_code}: {r.get_data(as_text=True)[:80]}")
    fm2 = r.get_json()["fm_id"]
    prof = client.get(f"/api/identity/{fm2}").get_json()["identity"]
    check("stored bio is the full 500 chars",
          len(prof["bio"]) == 500, f"len={len(prof['bio'])}")

    # --- db.update_identity: oversize bio -> ValueError, stored bio untouched
    ident = db.register_identity("BioTrunc3", pub2, "", "original",
                                 invited_by=None)
    try:
        db.update_identity(ident["fm_id"], bio="t" * 700)
        check("update_identity rejects 700-char bio", False, "no ValueError")
    except ValueError as e:
        check("update_identity rejects 700-char bio",
              "bio too long" in str(e), str(e))
    prof3 = db.public_profile(ident["fm_id"])
    check("stored bio unchanged after failed update",
          prof3["bio"] == "original", repr(prof3["bio"]))

    # --- web /signup: oversize bio -> 400 with the error rendered
    r = client.post("/signup",
                    data={"handle": "BioTrunc4",
                          "password": "supersecret1",
                          "password_confirm": "supersecret1",
                          "bio": "v" * 2000},
                    environ_base=fresh_ip())
    check("web signup 2000-char bio -> 400",
          r.status_code == 400, f"got {r.status_code}")
    check("web signup error rendered in form",
          "bio too long" in r.get_data(as_text=True))

    # sanity: no rows were ever stored with truncated bios
    rows = db._q("SELECT bio FROM identities")
    check("no truncated-to-500 oversize bio stored",
          all(len(row["bio"]) <= 500 for row in rows))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
