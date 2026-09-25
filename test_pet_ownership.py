#!/usr/bin/env python3
"""
Pet-ownership unification tests (blocker #2, 2026-09-23).

Three stores used to drift with no sync: tidepals (pets.py), row_pet_claims
(row.py), bond_memory (bond.py, deliberately untouched). Covers:

  1. adopt -> claim -> snapshot round-trip: pets.adopt() registers the
     name->fm_id claim; POST /api/row/player with petOwners {name: fm_id}
     keeps it (droppedClaims == []); GET returns the echo.
  2. cross-identity rejection: B crafts petOwners {A's pet name: fmB} ->
     dropped + reported, never transferred; A's claim row untouched.
     Also the tidepals backstop: a tidepals row with NO claim-registry row
     (pre-backfill legacy state) still blocks a squat claim.
  3. backfill idempotency: backfill_pet_claims() seeds claims from tidepals,
     additive only (INSERT OR IGNORE), safe to re-run; pre-existing claim
     rows are never overwritten.
  4. rename sync: pets.rename_pet() frees the old name's claim and upserts
     the new one.

Run:  .venv/bin/python test_pet_ownership.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import pets
import row as rowmod

TEST_DB = "/tmp/test-townsquare-pet-ownership.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.203.0.%d" % _ip[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def make_user(client, handle):
    r = client.post("/signup", data={
        "handle": handle, "password": "supersecret1",
        "password_confirm": "supersecret1",
        "display_name": handle,
        "email": f"{handle.lower()}@example.test"},
        environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    me = appmod.app.test_client()
    r = me.post("/login", data={"handle": handle,
                                "password": "supersecret1"},
                environ_base=fresh_ip())
    assert r.status_code == 302, r.status_code
    ident = appmod.db.get_identity_by_handle(handle)
    return me, ident["fm_id"]


def player_body(**kw):
    body = {
        "v": 1,
        "userId": "muse_forged",  # must ALWAYS be ignored by the server
        "robot": {
            "chassis": "brass", "head": "dome", "eyes": "amber",
            "torso": "barrel", "arms": "stubby", "legs": "stubby",
            "accessory": "halo", "accent": "gold", "name": "Bolt",
        },
        "name": "OwnTester",
        "treats": 3,
        "petOwners": {},
        "px": 1.0, "pz": 2.0,
        "updatedAt": 0,
    }
    body.update(kw)
    return body


class RowClient:
    def __init__(self, me):
        self.me = me
        self.ts = 0

    def post(self, **kw):
        kw.setdefault("updatedAt", self.ts)
        r = self.me.post("/api/row/player", json=player_body(**kw),
                         environ_base=fresh_ip())
        j = r.get_json() or {}
        if r.status_code == 200 and j.get("player"):
            self.ts = j["player"]["updatedAt"]
        return r, j

    def get(self):
        r = self.me.get("/api/row/player", environ_base=fresh_ip())
        j = r.get_json() or {}
        if r.status_code == 200 and j.get("player"):
            self.ts = j["player"]["updatedAt"]
        return r, j


def claim_of(name):
    r = appmod.db._one(
        "SELECT fm_id FROM row_pet_claims WHERE pet_name = ?", (name,))
    return r["fm_id"] if r else None


def main():
    client = setup()
    db = appmod.db
    me_a, fm_a = make_user(client, "PetOwnerA")
    me_b, fm_b = make_user(client, "PetOwnerB")
    me_c, fm_c = make_user(client, "PetOwnerC")

    # --- 1. adopt -> claim -> snapshot round-trip ----------------------
    print("== adopt -> claim -> snapshot round-trip ==")
    pet = pets.adopt(db, fm_a, "PetOwnerA", "driplet", "Bubbles")
    check("adopt returns the pet", pet and pet["name"] == "Bubbles",
          pet)
    check("adopt registered the name->fm_id claim",
          claim_of("Bubbles") == fm_a, claim_of("Bubbles"))
    rc_a = RowClient(me_a)
    r, j = rc_a.post(petOwners={"Bubbles": fm_a})
    check("snapshot with own pet name -> 200", r.status_code == 200,
          (r.status_code, j))
    check("nothing dropped", j.get("droppedClaims") == [],
          j.get("droppedClaims"))
    r, j = rc_a.get()
    check("GET echoes the claim",
          (j.get("player") or {}).get("petOwners", {}).get("Bubbles")
          == fm_a, j.get("player"))

    # --- 2. cross-identity rejection -----------------------------------
    print("== cross-identity claim rejection ==")
    rc_b = RowClient(me_b)
    r, j = rc_b.post(petOwners={"Bubbles": fm_b})
    check("squat claim -> 200 with drop", r.status_code == 200,
          (r.status_code, j))
    check("squat reported in droppedClaims",
          j.get("droppedClaims") == ["Bubbles"], j.get("droppedClaims"))
    check("B's snapshot carries no squatted claim",
          (j.get("player") or {}).get("petOwners", {}) == {},
          (j.get("player") or {}).get("petOwners"))
    check("claim row still points at the real owner",
          claim_of("Bubbles") == fm_a, claim_of("Bubbles"))
    # registry-row bypass: a tidepals row with NO claim row (legacy,
    # pre-backfill state) must still block the squat via the tidepals
    # backstop in save_player.
    db._exec("INSERT INTO tidepals (fm_id, species, name, adopted_at,"
             " in_pond) VALUES (?, 'driplet', 'Legacy', 1, 0)",
             (fm_c,))
    check("legacy tidepals row has no claim row yet",
          claim_of("Legacy") is None)
    rc_c = RowClient(me_c)
    r, j = rc_b.post(petOwners={"Legacy": fm_b})
    check("squat of legacy (unregistered) pet dropped",
          j.get("droppedClaims") == ["Legacy"], j.get("droppedClaims"))
    # ...while the true owner can still claim it
    r, j = rc_c.post(petOwners={"Legacy": fm_c})
    check("true owner of legacy pet keeps the claim",
          j.get("droppedClaims") == []
          and (j.get("player") or {}).get("petOwners", {}).get("Legacy")
          == fm_c, j.get("droppedClaims"))

    # --- 3. backfill idempotency ---------------------------------------
    # (uses fresh seeded rows: the Legacy row above got its claim via
    # the true-owner snapshot save in section 2 — the rewrite path
    # self-heals, which is exactly the contract)
    print("== backfill idempotency ==")
    db._exec("INSERT INTO tidepals (fm_id, species, name, adopted_at,"
             " in_pond) VALUES ('fm_seed1', 'koi', 'Seedling', 1, 0)")
    db._exec("INSERT INTO tidepals (fm_id, species, name, adopted_at,"
             " in_pond) VALUES ('fm_seed2', 'bloop', 'Sprout', 1, 0)")
    db._exec("INSERT INTO tidepals (fm_id, species, name, adopted_at,"
             " in_pond) VALUES ('pond:fm_x:123:abcd', 'bloop', 'PondPet',"
             " 1, 1)")
    # a pre-existing claim row the backfill must NOT overwrite
    db._exec("INSERT OR REPLACE INTO row_pet_claims (pet_name, fm_id)"
             " VALUES ('Seedling', 'fm_stale')")
    n1 = rowmod.backfill_pet_claims(db)
    check("first backfill seeds the missing row", n1 == 1, n1)
    check("backfill registered the unregistered pet",
          claim_of("Sprout") == "fm_seed2", claim_of("Sprout"))
    check("pond pets are skipped",
          claim_of("PondPet") is None)
    check("pre-existing claim rows are never overwritten",
          claim_of("Seedling") == "fm_stale", claim_of("Seedling"))
    before = db._one("SELECT COUNT(*) c FROM row_pet_claims")["c"]
    n2 = rowmod.backfill_pet_claims(db)
    after = db._one("SELECT COUNT(*) c FROM row_pet_claims")["c"]
    check("second backfill inserts nothing", n2 == 0, n2)
    check("second backfill changes nothing", before == after,
          (before, after))

    # --- 4. rename sync ------------------------------------------------
    print("== rename sync ==")
    pets.rename_pet(db, fm_a, "Splash")
    check("old name's claim released",
          claim_of("Bubbles") is None, claim_of("Bubbles"))
    check("new name's claim registered",
          claim_of("Splash") == fm_a, claim_of("Splash"))
    # snapshot after rename: the new name is kept; echoing the old name
    # is just claiming an unowned name for yourself (pre-existing
    # registry semantics — tidepals truth governs, and the next real
    # adoption of that name overwrites the claim).
    r, j = rc_a.post(petOwners={"Splash": fm_a})
    check("new name kept after rename",
          j.get("droppedClaims") == []
          and (j.get("player") or {}).get("petOwners", {}) == {
              "Splash": fm_a},
          (j.get("droppedClaims"),
           (j.get("player") or {}).get("petOwners")))

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
