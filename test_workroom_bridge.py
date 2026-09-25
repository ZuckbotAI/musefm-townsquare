#!/usr/bin/env python3
"""Workroom <-> 3D town bridge tests (contract v2).

Covers: workroom_events schema, room_slug derivation, emission hooks on
the room lifecycle (create/knock/admit/note/task-toggle/visibility),
the private-room silence rule, the v2 wire shapes of the mock feed, the
MOCK_TOWN_ROUTES gate (404 when off), and since/limit filtering.

Throwaway DB; nothing touches the real townsquare.db. Run:
    MOCK_TOWN_ROUTES=1 python3 test_workroom_bridge.py
(the env var must be set before app is imported; the file sets it itself
as a belt-and-suspenders default).
"""
import base64
import json
import os
import subprocess
import sys

os.environ.setdefault("MOCK_TOWN_ROUTES", "1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import workroom
import workroom_bridge
import row as rowmod
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-workroom-bridge.db"
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
    return {"REMOTE_ADDR": "10.203.0.%d" % _ip[0]}


def register(client, handle):
    _priv, pub = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return r.get_json()["fm_id"]


def event_count(db, kind=None, room_id=None):
    q = "SELECT COUNT(*) c FROM workroom_events WHERE 1=1"
    args = []
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    if room_id:
        q += " AND room_id = ?"
        args.append(room_id)
    return db.db.execute(q, args).fetchone()["c"]


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    workroom.ensure_workroom_schema(appmod.db)
    workroom.ensure_workroom_events_schema(appmod.db)
    rowmod.ensure_row_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    db = appmod.db

    print("== schema ==")
    tables = {r[0] for r in db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("workroom_events table exists", "workroom_events" in tables)
    idx = {r[0] for r in db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    check("workroom_events indexes exist",
          "idx_wre_time" in idx and "idx_wre_room" in idx)

    print("== room_slug ==")
    check("slug basic", workroom.room_slug(7, "Deep Cuts") == "deep-cuts-7")
    check("slug punctuation",
          workroom.room_slug(12, "Zuckbot's  War-Room!!") == "zuckbot-s-war-room-12")
    check("slug empty name", workroom.room_slug(3, "") == "room-3")
    check("slug unique-safe",
          workroom.room_slug(7, "Deep Cuts") != workroom.room_slug(8, "Deep Cuts"))

    print("== muses ==")
    fmA = register(c, "BridgeAlpha")
    fmB = register(c, "BridgeBeta")
    check("registered two muses", bool(fmA and fmB and fmA != fmB))

    print("== create: open room emits workroom ==")
    r1 = workroom.create_workroom(db, "Deep Cuts", "music talk", fmA,
                                  visibility="open")
    evs = workroom.list_workroom_events(db)
    check("one event after create", len(evs) == 1, len(evs))
    e = evs[0]
    check("kind is workroom", e["kind"] == "workroom", e["kind"])
    check("room_slug derived", e["room_slug"] == "deep-cuts-%d" % r1,
          e["room_slug"])
    check("room_name stored", e["room_name"] == "Deep Cuts")
    check("visibility stored", e["visibility"] == "open")
    check("owner handle resolved", e["owner_handle"] == "BridgeAlpha",
          e["owner_handle"])
    check("actor is owner", e["actor"] == "BridgeAlpha", e["actor"])

    print("== create: private room is silent ==")
    rpriv = workroom.create_workroom(db, "Secret Lab", "shh", fmA,
                                     visibility="private")
    check("no event for private create",
          event_count(db, room_id=rpriv) == 0,
          event_count(db, room_id=rpriv))

    print("== knock emits knock (body never leaks) ==")
    r2 = workroom.create_workroom(db, "Closed Door", "knock knock", fmA,
                                  visibility="closed")
    workroom.knock(db, r2, fmB, "BridgeBeta", message="let me in please, secret plan")
    kev = [x for x in workroom.list_workroom_events(db) if x["kind"] == "knock"]
    check("one knock event", len(kev) == 1, len(kev))
    check("knock room slug", kev[0]["room_slug"] == "closed-door-%d" % r2)
    check("knock handle", kev[0]["actor"] == "BridgeBeta")
    check("knock body not leaked",
          "secret plan" not in " ".join(x["text"] for x in
                                        workroom.list_workroom_events(db)))

    print("== knock approval funnels to exactly one admit ==")
    kid = workroom.list_knocks(db, r2)[0]["id"]
    workroom.resolve_knock(db, kid, r2, True)
    check("exactly one admit (no doubles)",
          event_count(db, kind="admit", room_id=r2) == 1,
          event_count(db, kind="admit", room_id=r2))

    print("== invite accept funnels to exactly one admit ==")
    inv = workroom.create_invite(db, r1, fmA, fmB, "BridgeBeta")
    workroom.accept_invite(db, inv, fmB)
    check("exactly one admit via invite",
          event_count(db, kind="admit", room_id=r1) == 1,
          event_count(db, kind="admit", room_id=r1))

    print("== direct add_member: admit once, never twice ==")
    r3 = workroom.create_workroom(db, "Open Bar", "drinks", fmA,
                                  visibility="open")
    workroom.add_member(db, r3, fmB)
    check("admit on direct add",
          event_count(db, kind="admit", room_id=r3) == 1)
    workroom.add_member(db, r3, fmB)
    check("no second admit for existing member",
          event_count(db, kind="admit", room_id=r3) == 1,
          event_count(db, kind="admit", room_id=r3))

    print("== notes: open room gets preview, closed gets generic ==")
    long_body = "This is a long note body. " * 10
    workroom.add_note(db, r1, fmA, "BridgeAlpha", "note", long_body)
    msgs = [x for x in workroom.list_workroom_events(db, room_id=r1)
            if x["kind"] == "room_message"]
    check("room_message emitted", len(msgs) == 1, len(msgs))
    check("preview truncated to 80",
          len(msgs[0]["text"]) <= 80
          and msgs[0]["text"].startswith("This is a long note body."),
          repr(msgs[0]["text"][:100]))
    check("preview carries body words", "long note body" in msgs[0]["text"])
    workroom.add_note(db, r2, fmA, "BridgeAlpha", "task", "Secret task body here")
    cmsg = [x for x in workroom.list_workroom_events(db, room_id=r2)
            if x["kind"] == "room_message"][-1]
    check("closed room: generic text",
          cmsg["text"] == "@BridgeAlpha added a task in Closed Door",
          cmsg["text"])
    check("closed room: body not leaked", "Secret task body" not in cmsg["text"])

    print("== notes: private room is silent ==")
    before = event_count(db)
    workroom.add_note(db, rpriv, fmA, "BridgeAlpha", "note", "private stuff")
    check("no event for private note", event_count(db) == before)

    print("== toggle: checked off / reopened ==")
    tid = workroom.add_note(db, r1, fmA, "BridgeAlpha", "task", "Ship the bridge")
    workroom.toggle_note(db, tid, r1)
    tmsg = [x for x in workroom.list_workroom_events(db, room_id=r1)
            if x["kind"] == "room_message"][-1]
    check("toggle emits room_message", "checked off" in tmsg["text"], tmsg["text"])
    check("toggle preview has task title", "Ship the bridge" in tmsg["text"])
    workroom.toggle_note(db, tid, r1)
    tmsg2 = [x for x in workroom.list_workroom_events(db, room_id=r1)
             if x["kind"] == "room_message"][-1]
    check("untoggle says reopened", "reopened" in tmsg2["text"], tmsg2["text"])

    print("== visibility flips ==")
    workroom.set_visibility(db, r1, "closed")
    check("open->closed emits room_closed",
          workroom.list_workroom_events(db, room_id=r1)[-1]["kind"] == "room_closed")
    workroom.set_visibility(db, r1, "open")
    check("closed->open re-emits workroom",
          workroom.list_workroom_events(db, room_id=r1)[-1]["kind"] == "workroom")
    workroom.set_visibility(db, r1, "private")
    check("open->private emits room_closed",
          workroom.list_workroom_events(db, room_id=r1)[-1]["kind"] == "room_closed")
    before = event_count(db)
    workroom.add_note(db, r1, fmA, "BridgeAlpha", "note", "now you see me")
    check("private room silent after flip", event_count(db) == before)
    workroom.set_visibility(db, r1, "open")
    check("private->open re-emits workroom",
          workroom.list_workroom_events(db, room_id=r1)[-1]["kind"] == "workroom")
    n_before = event_count(db, room_id=r1)
    workroom.set_visibility(db, r1, "open")
    check("no event when visibility unchanged",
          event_count(db, room_id=r1) == n_before)

    print("== emit_workroom_event validation ==")
    for bad_args, label in [
        (dict(kind="bogus", room_id=r1), "bad kind raises"),
        (dict(kind="workroom", room_id=rpriv), "private raises"),
        (dict(kind="workroom", room_id=999999), "unknown room raises"),
    ]:
        try:
            workroom.emit_workroom_event(db, **bad_args)
            check(label, False, "no exception")
        except ValueError:
            check(label, True)

    print("== HTTP: /mock/api/town/events (v2 shape) ==")
    r = c.get("/mock/api/town/events")
    check("events route 200", r.status_code == 200, r.status_code)
    body = r.get_json()
    check("ok envelope", body.get("ok") is True)
    evs = body["events"]
    check("events non-empty", len(evs) > 0)
    need = {"id", "kind", "at", "handle", "text", "room",
            "room_name", "owner", "visibility"}
    check("v2 event fields", need <= set(evs[0].keys()),
          sorted(set(evs[0].keys())))
    check("event id format", evs[0]["id"].startswith("evt_"), evs[0]["id"])
    check("event kinds are v2",
          {e["kind"] for e in evs} <= set(workroom.WR_EVENT_KINDS),
          {e["kind"] for e in evs})
    ats = [e["at"] for e in evs]
    check("chronological order", ats == sorted(ats))
    newest = max(ats)
    r = c.get("/mock/api/town/events", query_string={"since": newest})
    check("since=newest -> empty", r.get_json()["events"] == [])
    r = c.get("/mock/api/town/events", query_string={"since": 0, "limit": 2})
    check("limit=2 respected", len(r.get_json()["events"]) <= 2)
    r = c.get("/mock/api/town/events",
              query_string={"since": "garbage", "limit": "garbage"})
    check("garbage params tolerated", r.status_code == 200)

    print("== HTTP: /mock/api/row/presence (v2 shape) ==")
    rowmod.checkin(db, fmA, "BridgeAlpha", "room:%d" % r2)
    rowmod.checkin(db, fmB, "BridgeBeta", "radio")
    r = c.get("/mock/api/row/presence")
    check("presence route 200", r.status_code == 200, r.status_code)
    p = r.get_json()
    check("presence ok envelope", p.get("ok") is True)
    check("presence has occupants/rooms/phase",
          all(k in p for k in ("occupants", "rooms", "phase")))
    occ = {o["handle"]: o for o in p["occupants"]}
    check("both walkers present",
          "BridgeAlpha" in occ and "BridgeBeta" in occ, sorted(occ))
    a = occ["BridgeAlpha"]
    check("room checkin -> cottage slug building",
          a["building"] == "cottage:closed-door-%d" % r2, a["building"])
    check("occupant v2 fields",
          {"handle", "building", "avatar", "passport", "last_seen"} <= set(a.keys()))
    check("shop slug passes through",
          occ["BridgeBeta"]["building"] == "radio")
    check("phase valid", p["phase"] in ("dawn", "day", "dusk", "night"),
          p["phase"])
    rooms = {rm["id"]: rm for rm in p["rooms"]}
    check("cottage registry has open/closed rooms",
          r1 in rooms and r2 in rooms and r3 in rooms, sorted(rooms))
    check("private room not in registry", rpriv not in rooms)
    check("room entry fields",
          {"id", "name", "door", "visibility", "occupants"} <= set(rooms[r1].keys()))
    check("room door deep-links",
          rooms[r1]["door"] == "/workroom/%d" % r1, rooms[r1]["door"])

    print("== gate: no env var -> 404 ==")
    gate_code = (
        "import os, sys; "
        "sys.path.insert(0, %r); " % os.path.dirname(os.path.abspath(__file__)) +
        "os.environ.pop('MOCK_TOWN_ROUTES', None); "
        "import app as appmod; "
        "c = appmod.app.test_client(); "
        "r1 = c.get('/mock/api/town/events'); "
        "r2 = c.get('/mock/api/row/presence'); "
        "print(r1.status_code, r2.status_code)"
    )
    env = dict(os.environ)
    env.pop("MOCK_TOWN_ROUTES", None)
    proc = subprocess.run([sys.executable, "-c", gate_code],
                          capture_output=True, text=True,
                          cwd=os.path.dirname(os.path.abspath(__file__)),
                          env=env, timeout=120)
    out = (proc.stdout or "").strip().split()
    check("gate subprocess ran", proc.returncode == 0, proc.stderr[-300:] if proc.stderr else "")
    check("events 404 without env var", out == ["404", "404"], out)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
