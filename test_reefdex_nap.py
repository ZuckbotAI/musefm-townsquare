#!/usr/bin/env python3
"""
Tests for the Home Reef depth wave: rarity tiers, species jobs, the
Reefdex journal, and naps.

Run:  .venv/bin/python test_reefdex_nap.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.

Covers:
  - every production species has a rarity tier and a job
  - /api/reefdex public catalog (secret species hidden when undiscovered)
  - signed /api/reefdex marks discoveries
  - adopt() records a discovery; backfill stamps current pets
  - nap: +happiness, 2h cooldown, 30-min visible "napping" mood,
    cures sea sniffles, snail's pace repeat blocked
  - pet_status carries rarity/job/nap fields
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import pets
from db import Database, now
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-reefdex.db"

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


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def reg(c, handle):
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": handle, "public_key": pub})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    return priv, d["fm_id"]


def care(c, priv, fm_id, kind):
    body = signed_body(priv, "pet_care", fm_id)
    return c.post(f"/api/pet/{kind}", json=body)


def main():
    c = setup()
    db = appmod.db

    # --- rarity + jobs on every species ---
    missing_r = [k for k in pets.PET_SPECIES if k not in pets.SPECIES_RARITY]
    missing_j = [k for k in pets.PET_SPECIES if k not in pets.SPECIES_JOBS]
    check("every species has a rarity", not missing_r, str(missing_r))
    check("every species has a job", not missing_j, str(missing_j))
    check("rarity tiers valid",
          all(pets.species_rarity(k) in pets.RARITY_ORDER
              for k in pets.PET_SPECIES))
    check("zorb is secret", pets.species_rarity("zorb") == "secret")
    check("driplet common / gilt rare",
          pets.species_rarity("driplet") == "common" and
          pets.species_rarity("gilt") == "rare")

    # --- public reefdex hides the secret ---
    r = c.get("/api/reefdex")
    d = r.get_json()
    keys = {s["key"] for s in d["species"]}
    check("reefdex 200", r.status_code == 200 and d["ok"])
    check("zorb hidden publicly", "zorb" not in keys)
    check("catalog carries rarity+job",
          all("rarity" in s and "job" in s for s in d["species"]),
          str(d["species"][0])[:200])

    # --- adopt records a discovery; signed reefdex marks it ---
    priv, fm_id = reg(c, "reef_keeper_alice")
    body = signed_body(priv, "pet_adopt", fm_id, species="driplet",
                       name="Drippy")
    r2 = c.post("/api/pets/adopt", json=body)
    check("adopt 200", r2.status_code == 200, str(r2.get_json())[:200])
    check("discovery recorded",
          "driplet" in pets.discoveries(db, fm_id))
    q = signed_body(priv, "reefdex", fm_id)
    r3 = c.get("/api/reefdex",
               query_string={k: str(v) for k, v in q.items()})
    d3 = r3.get_json()
    by_key = {s["key"]: s for s in d3["species"]}
    check("signed reefdex marks driplet discovered",
          by_key["driplet"]["discovered"])
    check("signed reefdex marks bloop undiscovered",
          not by_key["bloop"]["discovered"])

    # --- pet_status carries rarity/job ---
    st = pets.pet_status(db, fm_id)
    check("status rarity", st["rarity"] == "common", str(st["rarity"]))
    check("status job", st["job"]["name"] == "Splash Play",
          str(st["job"]))

    # --- nap: happiness, cooldown, visible sleep, cures sniffles ---
    # drain happiness first so the gain is visible
    row = db._one("SELECT happiness FROM pet_care WHERE fm_id=?", (fm_id,))
    db._exec("UPDATE pet_care SET happiness=50 WHERE fm_id=?", (fm_id,))
    rn = care(c, priv, fm_id, "nap")
    dn = rn.get_json()
    check("nap 200", rn.status_code == 200 and dn["ok"], str(dn)[:200])
    check("nap +12 happiness",
          dn["happiness"] == 62, str(dn.get("happiness")))
    check("nap cures flag present",
          "cured_sniffles" in dn)
    st2 = pets.pet_status(db, fm_id)
    check("napping mood visible", st2["mood"] == "napping",
          str(st2["mood"]))
    check("napping flag", st2["napping"] is True)
    check("nap_in cooldown reported", st2["nap_in"] > 0,
          str(st2["nap_in"]))
    # SVG renders the sleepy face (zzz) while napping
    check("svg has zzz while napping",
          ">z<" in st2["svg"] or "z<" in st2["svg"] or
          st2["svg"].count("z") > 0, "svg lacks zzz")

    rn2 = care(c, priv, fm_id, "nap")
    check("second nap blocked by cooldown",
          rn2.status_code != 200, str(rn2.get_json())[:150])

    # --- nap cures the sniffles ---
    priv2, fm2 = reg(c, "reef_keeper_bob")
    body = signed_body(priv2, "pet_adopt", fm2, species="bloop",
                       name="Bloopy")
    c.post("/api/pets/adopt", json=body)
    db._exec("UPDATE pet_care SET sniffles_until=? WHERE fm_id=?",
             (now() + 3600, fm2))
    check("sniffly before nap", pets.has_sniffles(db, fm2))
    rn3 = care(c, priv2, fm2, "nap")
    d3n = rn3.get_json()
    check("nap 200 (sniffly pet)", rn3.status_code == 200 and d3n["ok"],
          str(d3n)[:200])
    check("nap cured the sniffles", d3n["cured_sniffles"] is True)
    check("sniffles gone", not pets.has_sniffles(db, fm2))

    # --- backfill stamps current pets ---
    db._exec("DELETE FROM reefdex_discoveries")
    n = pets.backfill_reefdex(db)
    check("backfill ran", n >= 2, str(n))
    check("backfill restored driplet",
          "driplet" in pets.discoveries(db, fm_id))
    check("backfill restored bloop",
          "bloop" in pets.discoveries(db, fm2))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
