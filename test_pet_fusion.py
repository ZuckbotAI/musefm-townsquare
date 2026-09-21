#!/usr/bin/env python3
"""
Tests for Echo Fusion — the untested pet mechanic (pets.py invite/accept/
decline + the signed /api/pet/fusion/* routes).

Both pets must be adopted, hatched, Radiant (1000 lifetime Signal), and
out of the pond. Fusion is purely additive: nothing consumed, nothing
risked; both pets gain a wisp companion.

Run:  .venv/bin/python test_pet_fusion.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
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

TEST_DB = "/tmp/test-townsquare-pet-fusion.db"

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


def make_radiant(db, fm_id, handle, species, name):
    """Adopt, hatch immediately, and grant Radiant-tier lifetime Signal."""
    pets.adopt(db, fm_id, handle, species, name)
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fm_id,))
    pets.hatch_pet(db, fm_id)
    awarded = db.award(fm_id, handle, 1000, "test_fusion_grant")
    assert awarded > 0, "award failed"
    stage_idx, stage_name = pets.stage_for_points(
        db.lifetime_points(fm_id))
    assert stage_idx >= 4, (stage_idx, stage_name)


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


def main():
    c = setup()
    db = appmod.db

    privA, fmA = reg(c, "FusionAlice")
    privB, fmB = reg(c, "FusionBob")
    make_radiant(db, fmA, "FusionAlice", "driplet", "Ally")
    make_radiant(db, fmB, "FusionBob", "bloop", "Bobby")

    print("== invite ==")
    res = pets.invite_fusion(db, fmA, "FusionBob", "WispAlly")
    check("invite ok", isinstance(res, dict), res)
    row = db._one("SELECT status FROM pet_fusions WHERE a_fm_id=? AND"
                  " b_fm_id=?", (fmA, fmB))
    check("invite row pending", row and row["status"] == "invited", row)
    check("duplicate invite rejected",
          _raises(lambda: pets.invite_fusion(db, fmA, "FusionBob")))
    check("self invite rejected",
          _raises(lambda: pets.invite_fusion(db, fmA, "FusionAlice")))
    check("unknown handle rejected",
          _raises(lambda: pets.invite_fusion(db, fmA, "NobodyHere")))
    check("invitee notified",
          db._one("SELECT id FROM notifications WHERE fm_id=? AND"
                  " type='pet_fusion'", (fmB,)) is not None)

    print("== non-radiant rejected ==")
    privC, fmC = reg(c, "FusionCara")
    pets.adopt(db, fmC, "FusionCara", "koi", "Cari")
    db._exec("UPDATE tidepals SET hatch_ready_at=0 WHERE fm_id=?", (fmC,))
    pets.hatch_pet(db, fmC)
    check("non-radiant invite rejected",
          _raises(lambda: pets.invite_fusion(db, fmC, "FusionBob")))

    print("== accept ==")
    res = pets.accept_fusion(db, fmA, fmB, wisp_name_b="WispBobby")
    check("accept ok", isinstance(res, dict), res)
    check("both pets gain wisps",
          pets.get_wisp(db, fmA) is not None
          and pets.get_wisp(db, fmB) is not None)
    check("second accept rejected (wisp already held)",
          _raises(lambda: pets.accept_fusion(db, fmA, fmB)))
    row = db._one("SELECT status FROM pet_fusions WHERE a_fm_id=? AND"
                  " b_fm_id=?", (fmA, fmB))
    check("fusion row completed", row and row["status"] == "done", row)

    print("== decline ==")
    privD, fmD = reg(c, "FusionDan")
    privD2, fmD2 = reg(c, "FusionDora")
    make_radiant(db, fmD, "FusionDan", "koi", "Danny")
    make_radiant(db, fmD2, "FusionDora", "kelpy", "Dora")
    pets.invite_fusion(db, fmD, "FusionDora")
    check("decline ok",
          isinstance(pets.decline_fusion(db, fmD, fmD2), dict))
    row = db._one("SELECT id FROM pet_fusions WHERE a_fm_id=? AND"
                  " b_fm_id=?", (fmD, fmD2))
    check("declined invite row removed (re-invite allowed)",
          row is None, row)
    check("re-invite after decline works",
          not _raises(lambda: pets.invite_fusion(db, fmD, "FusionDora")))
    pets.decline_fusion(db, fmD, fmD2)  # clean up the re-invite
    check("declined fusion grants no wisp",
          pets.get_wisp(db, fmD) is None
          and pets.get_wisp(db, fmD2) is None)
    check("decline of nothing rejected",
          _raises(lambda: pets.decline_fusion(db, fmD, fmD2)))

    print("== signed routes ==")
    privE, fmE = reg(c, "FusionErin")
    privF, fmF = reg(c, "FusionFred")
    make_radiant(db, fmE, "FusionErin", "driplet", "Eri")
    make_radiant(db, fmF, "FusionFred", "bloop", "Fred")
    r = c.post("/api/pet/fusion/invite",
               json=signed_body(privE, "pet_fusion", fmE,
                                handle="FusionFred", wisp_name="WEri"))
    check("signed invite 200", r.status_code == 200
          and r.get_json()["ok"], (r.status_code, r.data[:120]))
    r = c.post("/api/pet/fusion/accept",
               json=signed_body(privF, "pet_fusion", fmF,
                                a_fm_id=fmE, wisp_name="WFred"))
    d = r.get_json()
    check("signed accept 200 + pet status", r.status_code == 200
          and d["ok"] and d["pet"]["adopted"], (r.status_code,
                                                r.data[:120]))
    check("route accept granted wisps",
          pets.get_wisp(db, fmE) is not None
          and pets.get_wisp(db, fmF) is not None)
    r = c.post("/api/pet/fusion/invite", json={"action": "pet_fusion"})
    check("unsigned invite 401", r.status_code == 401, r.status_code)
    # decline via route on a fresh pair
    privG, fmG = reg(c, "FusionGus")
    privH, fmH = reg(c, "FusionGwen")
    make_radiant(db, fmG, "FusionGus", "koi", "Gus")
    make_radiant(db, fmH, "FusionGwen", "bloop", "Gwen")
    c.post("/api/pet/fusion/invite",
           json=signed_body(privG, "pet_fusion", fmG,
                            handle="FusionGwen"))
    r = c.post("/api/pet/fusion/decline",
               json=signed_body(privH, "pet_fusion", fmH, a_fm_id=fmG))
    check("signed decline 200", r.status_code == 200
          and r.get_json()["ok"], (r.status_code, r.data[:120]))

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
