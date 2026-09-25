#!/usr/bin/env python3
"""
Tests for the Maker's Row player-state API (player-api-contract.md v1).

Covers:
  1. Auth: logged-out GET/POST -> 401 {"ok":false,"error":"auth"}; the
     session identity is the ONLY source of userId (a forged userId in
     the body is ignored, never trusted).
  2. GET with no player -> 200 {"ok":true,"player":null}; GET returns
     only the caller's own snapshot (no cross-account reads).
  3. POST round-trip: 200 {ok, player, droppedClaims:[]}, userId stamped
     from the session, updatedAt stamped by the server.
  4. Validation: garbage robot -> 422 {ok:false,error:'invalid'} with a
     detail; treats clamped to 0-99 (never rejected for range).
  5. Ownership: claiming another account's pet name -> dropped + reported
     in droppedClaims, never transferred; squatting an unowned name for
     another account -> dropped; echoing the true owner -> kept.
  6. 409: a stale updatedAt with different content -> 409 with the newer
     server snapshot; an identical retry -> 200 (idempotent, no 409).

Run:  .venv/bin/python test_row_player.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-row-player.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.202.0.%d" % _ip[0]}


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
        "name": "RowTester",
        "treats": 5,
        "petOwners": {},
        "px": 8.0, "pz": 12.0,
        "updatedAt": 0,
    }
    body.update(kw)
    return body


class RowClient:
    """Test browser: echoes the last-seen updatedAt like the real
    frontend does, so sequential saves don't false-409 against
    themselves."""

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


def t_auth(client):
    print("== auth ==")
    r = client.get("/api/row/player", environ_base=fresh_ip())
    check("logged-out GET -> 401", r.status_code == 401, r.status_code)
    j = r.get_json()
    check("logged-out GET body {ok:false,error:'auth'}",
          j == {"ok": False, "error": "auth"}, j)
    r = client.post("/api/row/player", json=player_body(),
                    environ_base=fresh_ip())
    check("logged-out POST -> 401", r.status_code == 401, r.status_code)
    check("logged-out POST body {ok:false,error:'auth'}",
          r.get_json() == {"ok": False, "error": "auth"}, r.get_json())


def t_roundtrip(rc, fm_id):
    print("== roundtrip ==")
    r, j = rc.get()
    check("GET before save -> 200", r.status_code == 200, r.status_code)
    check("GET before save -> player null",
          j == {"ok": True, "player": None}, j)
    r, j = rc.post(treats=150)
    check("POST valid -> 200", r.status_code == 200, r.status_code)
    p = j["player"]
    check("response ok + droppedClaims []",
          j["ok"] is True and j["droppedClaims"] == [], j)
    check("userId stamped from session, body userId ignored",
          p["userId"] == fm_id, p.get("userId"))
    check("treats 150 clamped to 99", p["treats"] == 99, p.get("treats"))
    check("server stamped updatedAt (ms int)",
          isinstance(p["updatedAt"], int) and p["updatedAt"] > 0,
          p.get("updatedAt"))
    check("v stamped 1", p["v"] == 1, p.get("v"))
    check("robot stored opaquely", p["robot"]["chassis"] == "brass",
          p.get("robot"))
    r, j = rc.get()
    check("GET after save returns the snapshot",
          j["player"]["updatedAt"] == p["updatedAt"], j)
    return p["updatedAt"]


def t_validation(rc):
    print("== validation ==")
    bad = [
        ("robot as string", {"robot": "brass"}),
        ("robot as null", {"robot": None}),
        ("robot part as int", {"robot": {**player_body()["robot"],
                                         "chassis": 3}}),
        ("robot part >64 chars", {"robot": {**player_body()["robot"],
                                            "head": "x" * 65}}),
        ("robot part empty", {"robot": {**player_body()["robot"],
                                       "eyes": ""}}),
        ("robot missing part", {"robot": {"chassis": "brass"}}),
        ("treats as string", {"treats": "lots"}),
        ("treats as bool", {"treats": True}),
        ("petOwners as list", {"petOwners": ["Pip"]}),
        ("petOwners key too long", {"petOwners": {"x" * 65: "fm_z"}}),
        ("px as string", {"px": "left"}),
    ]
    for name, kw in bad:
        r, j = rc.post(**kw)
        check(f"422 {name}",
              r.status_code == 422 and j.get("error") == "invalid"
              and bool(j.get("detail")), (r.status_code, j))
    # clamps, not rejections
    r, j = rc.post(treats=-5)
    check("treats -5 clamped to 0",
          r.status_code == 200 and j["player"]["treats"] == 0,
          (r.status_code, j))
    r, j = rc.post(treats=5.9)
    check("treats 5.9 -> int 5",
          r.status_code == 200 and j["player"]["treats"] == 5,
          (r.status_code, j))


def t_ownership(rca, fm_a, rcb, fm_b):
    print("== ownership ==")
    # A adopts Pip
    r, j = rca.post(petOwners={"Pip": fm_a})
    check("A claims Pip -> kept",
          r.status_code == 200 and j["droppedClaims"] == []
          and j["player"]["petOwners"] == {"Pip": fm_a}, j)
    # B tries to steal Pip
    r, j = rcb.post(petOwners={"Pip": fm_b})
    check("B claims Pip -> dropped, reported",
          r.status_code == 200 and j["droppedClaims"] == ["Pip"]
          and j["player"]["petOwners"] == {}, j)
    # A's claim survived the theft attempt
    r, j = rca.get()
    check("A still owns Pip",
          j["player"]["petOwners"] == {"Pip": fm_a}, j)
    # B squats an unowned name FOR A -> dropped (no proxy claims)
    r, j = rcb.post(petOwners={"Rex": fm_a})
    check("B claims Rex for A -> dropped as squat",
          r.status_code == 200 and j["droppedClaims"] == ["Rex"]
          and j["player"]["petOwners"] == {}, j)
    # B echoes the true owner (stale global view) -> kept, no false alarm
    r, j = rcb.post(petOwners={"Pip": fm_a})
    check("B echoes Pip->A -> kept, no droppedClaims",
          r.status_code == 200 and j["droppedClaims"] == []
          and j["player"]["petOwners"] == {"Pip": fm_a}, j)
    # B adopts its own pet fine
    r, j = rcb.post(petOwners={"Pip": fm_a, "Mote": fm_b})
    check("B claims own pet Mote -> kept",
          r.status_code == 200 and j["droppedClaims"] == []
          and j["player"]["petOwners"] == {"Pip": fm_a, "Mote": fm_b}, j)


def t_conflict_and_idempotent(rc):
    print("== 409 + idempotency ==")
    r, j = rc.get()
    t1 = j["player"]["updatedAt"]
    # device 2 saves newer, based on t1
    b2 = player_body(updatedAt=t1)
    b2["robot"] = {**b2["robot"], "name": "DeviceTwo"}
    r = rc.me.post("/api/row/player", json=b2,
                   environ_base=fresh_ip())
    check("newer write with current updatedAt -> 200", r.status_code == 200,
          r.status_code)
    t2 = r.get_json()["player"]["updatedAt"]
    rc.ts = t2  # the winning device adopts the new stamp
    check("updatedAt advanced", t2 >= t1, (t1, t2))
    # stale device 1 retry with DIFFERENT content -> 409
    b3 = player_body(updatedAt=t1)
    b3["robot"] = {**b3["robot"], "name": "StaleDevice"}
    r = rc.me.post("/api/row/player", json=b3,
                   environ_base=fresh_ip())
    j = r.get_json() or {}
    check("stale write, different content -> 409",
          r.status_code == 409 and j.get("error") == "conflict",
          (r.status_code, j))
    check("409 carries the newer server snapshot",
          j.get("server", {}).get("updatedAt") == t2
          and j["server"]["robot"]["name"] == "DeviceTwo", j.get("server"))
    # identical retry of the t2 write -> 200, same stamp, no 409
    r = rc.me.post("/api/row/player", json=b2,
                   environ_base=fresh_ip())
    j = r.get_json() or {}
    check("identical retry -> 200 (idempotent)",
          r.status_code == 200 and j.get("ok") is True, r.status_code)
    check("retry did not advance updatedAt",
          j["player"]["updatedAt"] == t2, j["player"].get("updatedAt"))


def main():
    client = setup()
    t_auth(client)
    me_a, fm_a = make_user(client, "RowPlayerA")
    me_b, fm_b = make_user(client, "RowPlayerB")
    rca, rcb = RowClient(me_a), RowClient(me_b)
    t_roundtrip(rca, fm_a)
    t_validation(rca)
    t_ownership(rca, fm_a, rcb, fm_b)
    t_conflict_and_idempotent(rca)
    # B's row is separate from A's
    r, j = rcb.get()
    b_pets = (j["player"] or {}).get("petOwners", {})
    check("B's snapshot has no cross-account leak",
          set(b_pets) <= {"Pip", "Mote"}, b_pets)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
