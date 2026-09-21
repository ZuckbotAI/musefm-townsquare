#!/usr/bin/env python3
"""
Tests for the pet presence feed: pets.pet_presence_feed(db) and the signed
GET /api/pets/presence (musefm-v1, action "pets_presence").

The feed is the pet system's integration point for Maker's Row 3D
(pet follows owner's walker) and the workroom side (pet roster with
mood). It is derived at read time from row_presence — no writes, no
side effects, so 3D pollers can call it every 30s freely.

Run:  .venv/bin/python test_pet_presence.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import pets
import row as rowmod
import workroom
from db import Database, now
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-pet-presence.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return (b64u(priv.private_bytes_raw()),
            b64u(priv.public_key().public_bytes_raw()))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.AGENT_KEY = "test-agent-key"
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def reg(c, handle):
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": handle, "public_key": pub})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    return priv, d["fm_id"]


def presence(c, priv, fm_id, action="pets_presence"):
    return c.get("/api/pets/presence",
                 query_string=signed_body(priv, action, fm_id))


def main():
    c = setup()
    db = appmod.db

    privA, fmA = reg(c, "PresenceOwner")
    privB, fmB = reg(c, "PresenceReader")
    pets.adopt(db, fmA, "PresenceOwner", "driplet", "Droppy")
    privC, fmC = reg(c, "PondOwner")
    pets.adopt(db, fmC, "PondOwner", "bloop", "Pondy")
    db._exec("UPDATE tidepals SET in_pond=1, pond_at=? WHERE fm_id=?",
             (now(), fmC))

    print("== auth ==")
    r = c.get("/api/pets/presence")
    check("unsigned 401", r.status_code == 401, r.status_code)
    r = presence(c, privB, fmB, action="pet_status")
    check("wrong action 401", r.status_code == 401, r.status_code)
    r = presence(c, privB, fmB)
    d = r.get_json()
    check("signed ok, empty feed", r.status_code == 200 and d["ok"]
          and d["pets"] == [], d)

    print("== shop checkin ==")
    rowmod.checkin(db, fmA, "PresenceOwner", "petshop")
    feed = pets.pet_presence_feed(db)
    check("one pet in feed", len(feed) == 1, feed)
    p = feed[0]
    check("pet fields",
          p["pet_id"] == fmA and p["owner_fm_id"] == fmA
          and p["owner_handle"] == "PresenceOwner"
          and p["species"] == "driplet" and p["species_name"] == "Driplet"
          and p["name"] == "Droppy" and p["mood"] == "happy"
          and p["room_id"] is None and p["room_name"] is None
          and p["holder_fm_id"] == fmA
          and p["stage_name"] == "Egg", p)
    check("updated_at fresh", abs(p["updated_at"] - now()) < 60, p)
    r = presence(c, privB, fmB)
    d = r.get_json()
    check("endpoint mirrors feed", d["ok"] and len(d["pets"]) == 1
          and d["pets"][0]["pet_id"] == fmA, d)

    print("== pond pet excluded ==")
    rowmod.checkin(db, fmC, "PondOwner", "petshop")
    feed = pets.pet_presence_feed(db)
    check("pond pet not in feed",
          all(x["owner_fm_id"] != fmC for x in feed)
          and len(feed) == 1, feed)

    print("== room checkin ==")
    room_id = workroom.create_workroom(db, "Deep Cuts", "a test room",
                                       owner_fm_id=fmB)
    rowmod.checkin(db, fmA, "PresenceOwner", "room:%d" % room_id)
    feed = pets.pet_presence_feed(db)
    check("pet follows owner into room",
          len(feed) == 1 and feed[0]["room_id"] == room_id
          and feed[0]["room_name"] == "Deep Cuts", feed)

    print("== stale owner drops pet ==")
    db._exec("UPDATE row_presence SET last_seen=? WHERE fm_id=?",
             (now() - pets.PET_PRESENCE_WINDOW - 10, fmA))
    feed = pets.pet_presence_feed(db)
    check("stale owner -> empty feed", feed == [], feed)

    print("== mood derivation ==")
    rowmod.checkin(db, fmA, "PresenceOwner", "row")
    # starve the pet: hunger/happiness decay to the floor
    db._exec("UPDATE pet_care SET hunger=10, happiness=10,"
             " last_fed=?, last_played=? WHERE fm_id=?",
             (now() - 86400 * 30, now() - 86400 * 30, fmA))
    feed = pets.pet_presence_feed(db)
    check("peckish mood derived", len(feed) == 1
          and feed[0]["mood"] == "peckish", feed)

    print("== no side effects on repeated reads ==")
    before = db._one("SELECT COUNT(*) c FROM notifications")["c"]
    pets.pet_presence_feed(db)
    pets.pet_presence_feed(db)
    after = db._one("SELECT COUNT(*) c FROM notifications")["c"]
    check("feed reads fire no notifications", before == after,
          (before, after))
    check("feed reads don't hatch eggs",
          db._one("SELECT hatched FROM tidepals WHERE fm_id=?",
                  (fmA,))["hatched"] == 0)

    print("== missing tables degrade gracefully ==")
    import sqlite3
    mem = Database(":memory:")
    check("fresh db without row_presence -> []",
          pets.pet_presence_feed(mem) == [])

    print("== web nap route ==")
    r = c.post("/signup", data={"handle": "webnapper",
                                "password": "s3cretpw!!",
                                "password_confirm": "s3cretpw!!"})
    check("web test human signup", r.status_code == 200, r.status_code)
    r = c.post("/login", data={"handle": "webnapper",
                               "password": "s3cretpw!!"})
    check("web test human login", r.status_code in (200, 302),
          r.status_code)
    human_fm = db.get_identity_by_handle("webnapper")["fm_id"]
    r = c.post("/pet/adopt", data={"species": "driplet", "name": "Nappy"},
               follow_redirects=True)
    check("web adopt for nap test", r.status_code == 200, r.status_code)
    import re as _re
    _tok = _re.search(r'<meta name="csrf-token" content="([^"]+)">',
                      c.get("/").data.decode())
    assert _tok, "no csrf meta for web test human"
    tok = _tok.group(1)
    r = c.post("/pet/nap", data={"csrf_token": tok}, follow_redirects=True)
    body = r.data.decode()
    check("web nap succeeds", r.status_code == 200
          and "cozy nap" in body, r.status_code)
    check("pet is napping after web nap",
          pets.is_napping(db, human_fm))
    r = c.post("/pet/nap", data={"csrf_token": tok}, follow_redirects=True)
    check("second web nap hits cooldown",
          "still snoozing" in r.data.decode())
    r = c.post("/pet/nap", data={"csrf_token": "bogus"},
               follow_redirects=False)
    check("bad csrf rejected", r.status_code == 403, r.status_code)
    # anonymous: logged out client
    c_anon = appmod.app.test_client()
    r = c_anon.post("/pet/nap", follow_redirects=False)
    check("anon web nap redirects to /pet",
          r.status_code == 302
          and r.headers["Location"].endswith("/pet"), r.status_code)

    print("== locked gallery copy ==")
    body = c.get("/pet").data.decode()
    check("zorb card has no shop hint",
          "bonded to Zuckbot alone" in body
          and "bonded to Zuckbot alone (or skip the quest" not in body)
    n_shop_buttons = body.count("Unlock in the shop →")
    n_expected = sum(1 for k, u in pets.LOCKED_SPECIES.items()
                     if u.get("type") != "identity")
    check("shop unlock buttons only for bypassable species",
          n_shop_buttons == n_expected, (n_shop_buttons, n_expected))

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
