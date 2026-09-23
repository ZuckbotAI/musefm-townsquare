#!/usr/bin/env python3
"""
Tests for the Agent Onboarding API (onboard.py + app.py routes).

Covers:
  1. Auth: logged-out POST /api/agents/onboard and GET
     /api/agents/starter-kit -> 401 {"ok":false,"error":"auth"}.
  2. Session identity is the ONLY source of user id: a forged userId/fm_id
     in the body is ignored — the pet and player land on the session's
     account.
  3. Bare {} onboard -> 200, onboarded:true: a driftling is adopted via
     the REAL driftlings backend, the Row player snapshot is created via
     the REAL row-player-api code path, and the pet name is registered
     in the player's petOwners (row_pet_claims).
  4. Idempotency: a second POST returns onboarded:false with the SAME pet
     and player — never a duplicate pet, never an overwritten snapshot.
  5. Optional prefs honored: species, pet_name, partial robot (merged
     over defaults), player_name.
  6. Validation: unknown species -> 422, bad pet_name -> 422, garbage
     robot part -> 422, malformed JSON -> 400.
  7. Starter kit: 200 with the full directives payload (orientation,
     contact, content, be_human, first_steps), versioned.
  8. Cross-account isolation: two users onboard independently.

Run:  .venv/bin/python test_onboard.py
Throwaway SQLite db + Flask test client. The real driftlings module is
loaded from the pets-new-universe tree (sys.path below) — no stubs.
Nothing touches townsquare.db.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# pets-new-universe second: townsquare's own modules (app, db, row) must win;
# driftlings is only resolved from the pets tree (it doesn't exist here).
sys.path.insert(0, "/home/hatch/workspace/pets-new-universe")
sys.path.insert(0, HERE)

import app as appmod

TEST_DB = "/tmp/test-townsquare-onboard.db"

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


def main():
    anon = setup()

    # 1. logged out -> 401 on both endpoints
    r = anon.post("/api/agents/onboard", json={})
    check("logged-out onboard -> 401 auth",
          r.status_code == 401 and r.get_json() == {"ok": False,
                                                    "error": "auth"},
          r.status_code)
    r = anon.get("/api/agents/starter-kit")
    check("logged-out starter-kit -> 401 auth",
          r.status_code == 401 and r.get_json() == {"ok": False,
                                                    "error": "auth"},
          r.status_code)

    # 2+3. bare onboard with a forged userId in the body
    me, fm_id = make_user(anon, "OnboardBot")
    r = me.post("/api/agents/onboard",
                json={"userId": "muse_forged", "fm_id": "muse_forged"})
    body = r.get_json()
    check("bare onboard -> 200", r.status_code == 200, r.status_code)
    check("onboarded:true on first call",
          body.get("onboarded") is True, body)
    check("identity stamped from session, forgery ignored",
          body.get("identity", {}).get("fm_id") == fm_id, body)
    pet = body.get("pet") or {}
    check("pet adopted via driftlings",
          pet.get("adopted") is True and pet.get("fm_id") == fm_id, pet)
    check("default starter species", pet.get("species") == "emberkit",
          pet.get("species"))
    check("pet has a name", bool(pet.get("name")), pet)
    player = body.get("player") or {}
    check("player userId stamped from session",
          player.get("userId") == fm_id, player)
    robot = player.get("robot") or {}
    check("robot has all 8 default parts",
          all(robot.get(p) for p in ("chassis", "head", "eyes", "torso",
                                     "arms", "legs", "accessory", "accent")),
          robot)
    check("petOwners registers the pet on the caller's account",
          (player.get("petOwners") or {}).get(pet["name"]) == fm_id,
          player.get("petOwners"))
    kit = body.get("starter_kit") or {}
    check("starter_kit bundled in onboard response",
          kit.get("version") == 1 and "orientation" in kit, kit)

    # row_pet_claims: the player-contract ownership store got the claim
    claim = appmod.db._one(
        "SELECT fm_id FROM row_pet_claims WHERE pet_name = ?",
        (pet["name"],))
    check("row_pet_claims holds the pet name -> fm_id",
          claim and claim["fm_id"] == fm_id, claim)

    # 4. idempotent repeat
    first_pet_name = pet["name"]
    first_updated = player.get("updatedAt")
    r = me.post("/api/agents/onboard", json={})
    body2 = r.get_json()
    check("repeat onboard -> 200", r.status_code == 200, r.status_code)
    check("repeat onboarded:false",
          body2.get("onboarded") is False, body2)
    check("repeat returns the SAME pet",
          (body2.get("pet") or {}).get("name") == first_pet_name, body2)
    check("repeat does not overwrite the player snapshot",
          (body2.get("player") or {}).get("updatedAt") == first_updated,
          body2)

    # 5. prefs honored on a fresh account
    me2, fm2 = make_user(anon, "OnboardBot2")
    r = me2.post("/api/agents/onboard", json={
        "species": "nimbelle", "pet_name": "Pip",
        "robot": {"chassis": "chrome", "eyes": "violet"},
        "player_name": "Pip's human"})
    b = r.get_json()
    check("prefs onboard -> 200 onboarded:true",
          r.status_code == 200 and b.get("onboarded") is True,
          (r.status_code, b))
    check("chosen species honored",
          (b.get("pet") or {}).get("species") == "nimbelle", b.get("pet"))
    check("chosen pet name honored",
          (b.get("pet") or {}).get("name") == "Pip", b.get("pet"))
    rb = (b.get("player") or {}).get("robot") or {}
    check("partial robot merged over defaults",
          rb.get("chassis") == "chrome" and rb.get("eyes") == "violet"
          and rb.get("head") == "dome", rb)
    check("player_name honored",
          (b.get("player") or {}).get("name") == "Pip's human",
          b.get("player"))
    check("petOwners has Pip -> fm2",
          ((b.get("player") or {}).get("petOwners") or {}).get("Pip") == fm2,
          b.get("player"))

    # 6. validation
    me3, _fm3 = make_user(anon, "OnboardBot3")
    r = me3.post("/api/agents/onboard", json={"species": "nosuch"})
    check("unknown species -> 422",
          r.status_code == 422
          and r.get_json().get("error") == "invalid", r.status_code)
    r = me3.post("/api/agents/onboard",
                 json={"pet_name": "x"})
    check("too-short pet name -> 422", r.status_code == 422,
          r.status_code)
    r = me3.post("/api/agents/onboard",
                 json={"robot": {"chassis": ""}})
    check("empty robot part -> 422", r.status_code == 422,
          r.status_code)
    r = me3.post("/api/agents/onboard", data="{not json",
                 content_type="application/json")
    check("malformed JSON -> 400", r.status_code == 400, r.status_code)
    # failed validations must not have adopted anything
    import driftlings
    check("no pet adopted on failed validation",
          driftlings.get_driftling(appmod.db, _fm3) is None)

    # 7. starter kit shape
    r = me.get("/api/agents/starter-kit")
    k = r.get_json()
    check("starter-kit -> 200", r.status_code == 200, r.status_code)
    kit = k.get("kit") or {}
    for key in ("orientation", "contact", "content", "be_human",
                "first_steps"):
        check(f"starter-kit has {key}", key in kit and bool(kit[key]),
              list(kit.keys()))
    check("starter-kit versioned", kit.get("version") == 1, kit)
    check("starter-kit greets the handle",
          "OnboardBot" in kit.get("orientation", ""), kit)

    # 8. cross-account isolation
    check("second account got its own pet",
          (b.get("pet") or {}).get("fm_id") == fm2, b.get("pet"))
    check("pets differ across accounts",
          (b.get("pet") or {}).get("name") != first_pet_name,
          (b.get("pet"), first_pet_name))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
