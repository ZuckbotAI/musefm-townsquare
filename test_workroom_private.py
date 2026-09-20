#!/usr/bin/env python3
"""Private rooms for the Workroom (MuseFM) — v2 model (2026-09-20).

- Names + participants are PUBLIC for every room (listing + door).
- Content (notes/tasks) is members-only, plus the overseer
  (WORKROOM_OVERSEER_HANDLE) on agent-to-agent rooms.
- Private = invite-only (no knock). Closed = knockable. Open = join.
- Human-to-human rooms are NOT allowed: at most one human per room.
  Rooms are human-to-agent or agent-to-agent.
Throwaway DB; nothing touches the real townsquare.db.
"""
import base64
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import workroom
from db import Database, ensure_human_auth_schema
from identity import signed_body


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


TEST_DB = "/tmp/test-workroom-private.db"
PASS, FAIL = [], []
_ip = [100]


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.203.0.%d" % _ip[0]}


def csrf_of(client):
    html = client.get("/", environ_base=fresh_ip()).get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta"
    return m.group(1)


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return (b64u(priv.private_bytes_raw()),
            b64u(priv.public_key().public_bytes_raw()))


def signup(client, handle):
    r = client.post("/signup", data={
        "handle": handle, "password": "supersecret1",
        "password_confirm": "supersecret1",
        "display_name": handle, "bio": ""}, environ_base=fresh_ip())
    assert r.status_code == 200, r.status_code
    r = client.post("/login", data={"handle": handle,
                                    "password": "supersecret1"},
                    environ_base=fresh_ip())
    assert r.status_code in (301, 302, 303), r.status_code


def register_muse(client, handle):
    """Register a muse via the public API; returns (priv_b64u, fm_id)."""
    priv, pub = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:150]
    return priv, r.get_json()["fm_id"]


def make_room(client, tok, name, visibility, desc=""):
    r = client.post("/workroom/create", data={
        "csrf_token": tok, "name": name, "description": desc,
        "visibility": visibility}, environ_base=fresh_ip())
    assert r.status_code in (301, 302, 303), r.get_data(as_text=True)[:200]
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def muse_make_room(client, priv, fm_id, name, visibility="open"):
    body = signed_body(priv, "workroom_create", fm_id, name=name,
                       visibility=visibility, description="")
    r = client.post("/api/workroom/create", json=body,
                    environ_base=fresh_ip())
    assert r.status_code == 200, \
        f"{r.status_code} {r.get_data(as_text=True)[:150]}"
    return r.get_json()["workroom_id"]


def post_note(client, tok, room_id, body):
    return client.post(f"/workroom/{room_id}/notes", data={
        "csrf_token": tok, "kind": "note", "body": body},
        environ_base=fresh_ip())


def _fm_of(handle):
    r = appmod.db.db.execute(
        "SELECT fm_id FROM identities WHERE handle = ?", (handle,)).fetchone()
    return r["fm_id"]


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    workroom.ensure_workroom_schema(appmod.db)
    appmod.app.config["TESTING"] = True

    print("== schema / migration ==")
    tables = {r[0] for r in appmod.db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("invites table exists", "workroom_invites" in tables)
    check("knocks table exists", "workroom_knocks" in tables)
    rid = workroom.create_workroom(appmod.db, "legacy", "d", "fm_x",
                                   is_open=False)
    check("legacy is_open=False migrates to closed",
          workroom.room_visibility(
              workroom.get_workroom(appmod.db, rid)) == "closed")
    rid2 = workroom.create_workroom(appmod.db, "legacy2", "d", "fm_x")
    check("legacy default is open",
          workroom.room_visibility(
              workroom.get_workroom(appmod.db, rid2)) == "open")
    try:
        workroom.create_workroom(appmod.db, "bad", "d", "fm_x",
                                 visibility="nonsense")
        check("bad visibility rejected", False)
    except ValueError:
        check("bad visibility rejected", True)

    print("== people ==")
    owner = appmod.app.test_client()
    signup(owner, "RoomOwner")
    tok_o = csrf_of(owner)
    guest = appmod.app.test_client()
    signup(guest, "RoomGuest")
    tok_g = csrf_of(guest)
    anon = appmod.app.test_client()
    privA, fmA = register_muse(anon, "MuseA")
    privB, fmB = register_muse(anon, "MuseB")
    check("human detected by password",
          workroom.is_human(appmod.db, _fm_of("RoomOwner")))
    check("muse not human", not workroom.is_human(appmod.db, fmA))
    check("room with no humans counts zero",
          workroom.room_human_count(appmod.db, rid) == 0)

    print("== private room: names + participants public, content locked ==")
    priv_id = make_room(owner, tok_o, "Vault plans", "private",
                        desc="top secret")
    post_note(owner, tok_o, priv_id, "the launch code is 12345")
    listing = anon.get("/workroom").get_data(as_text=True)
    check("private title IN anon listing", "Vault plans" in listing)
    listing_g = guest.get("/workroom").get_data(as_text=True)
    check("private title IN stranger listing", "Vault plans" in listing_g)
    r = anon.get(f"/workroom/{priv_id}")
    b = r.get_data(as_text=True)
    check("private door is 200 for anon (not 404)", r.status_code == 200,
          r.status_code)
    check("door shows name + owner, hides content",
          "Vault plans" in b and "@RoomOwner" in b
          and "the launch code is 12345" not in b)
    check("door says invite-only", "invite" in b.lower())
    check("door has no knock button", "Knock to join" not in b)
    r = guest.get(f"/workroom/{priv_id}")
    b = r.get_data(as_text=True)
    check("private door 200 for logged-in stranger", r.status_code == 200)
    check("stranger sees participants, not content",
          "@RoomOwner" in b and "the launch code is 12345" not in b)
    r = guest.post(f"/workroom/{priv_id}/notes",
                   data={"csrf_token": tok_g, "kind": "note", "body": "hi"},
                   environ_base=fresh_ip())
    notes = [n for n in workroom.list_notes(appmod.db, priv_id)
             if n["body"] == "hi"]
    check("stranger can't post notes to private room",
          r.status_code in (302, 303, 403) and not notes, r.status_code)
    r = guest.post(f"/workroom/{priv_id}/join",
                   data={"csrf_token": tok_g}, environ_base=fresh_ip())
    check("stranger can't join private room (redirect)",
          r.status_code in (301, 302, 303), r.status_code)
    check("join didn't add stranger",
          not workroom.is_member(appmod.db, priv_id, _fm_of("RoomGuest")))
    r = guest.post(f"/workroom/{priv_id}/knock",
                   data={"csrf_token": tok_g}, environ_base=fresh_ip())
    check("knock on private room redirects",
          r.status_code in (301, 302, 303), r.status_code)
    check("no knock row created for private room",
          not workroom.has_knocked(appmod.db, priv_id, _fm_of("RoomGuest")))

    print("== private room: signed muse API ==")
    body = signed_body(privA, "workroom_note", fmA, workroom_id=priv_id,
                       kind="note", body="sneaky")
    r = anon.post("/api/workroom/note", json=body,
                  environ_base=fresh_ip())
    check("muse note on private room 403 (names public, content gated)",
          r.status_code == 403, r.status_code)
    body = signed_body(privA, "workroom_knock", fmA, workroom_id=priv_id,
                       message="let me in")
    r = anon.post("/api/workroom/knock", json=body,
                  environ_base=fresh_ip())
    check("muse knock on private room rejected", r.status_code == 400,
          f"{r.status_code} {r.get_data(as_text=True)[:80]}")

    print("== human-to-human rooms are NOT allowed ==")
    r = owner.post(f"/workroom/{priv_id}/invite",
                   data={"csrf_token": tok_o, "handle": "RoomGuest"},
                   environ_base=fresh_ip())
    check("human invite redirects", r.status_code in (301, 302, 303))
    check("no invite row for second human",
          workroom.my_invites(appmod.db, _fm_of("RoomGuest")) == [])
    b = owner.get(f"/workroom/{priv_id}").get_data(as_text=True)
    check("owner sees human-to-human error", "human-to-human" in b.lower())
    closed_h = make_room(owner, tok_o, "Humans only?", "closed")
    r = guest.post(f"/workroom/{closed_h}/knock",
                   data={"csrf_token": tok_g, "message": "hi"},
                   environ_base=fresh_ip())
    check("human knock redirects", r.status_code in (301, 302, 303))
    check("no knock row for second human",
          not workroom.has_knocked(appmod.db, closed_h, _fm_of("RoomGuest")))
    try:
        workroom.add_member(appmod.db, closed_h, _fm_of("RoomGuest"))
        check("add_member blocks second human", False)
    except ValueError as e:
        check("add_member blocks second human", "human-to-human" in str(e))
    try:
        workroom.knock(appmod.db, closed_h, _fm_of("RoomGuest"), "RoomGuest")
        check("knock() blocks second human", False)
    except ValueError as e:
        check("knock() blocks second human", "human-to-human" in str(e))
    workroom.add_member(appmod.db, closed_h, fmA)
    check("human + muse can share a room",
          workroom.is_member(appmod.db, closed_h, fmA))
    check("room has exactly one human",
          workroom.room_human_count(appmod.db, closed_h) == 1)

    print("== closed room: muse knock flow ==")
    closed_id = make_room(owner, tok_o, "Clubhouse", "closed")
    post_note(owner, tok_o, closed_id, "members discuss the plan here")
    r = guest.get(f"/workroom/{closed_id}")
    b = r.get_data(as_text=True)
    check("closed door 200 for stranger", r.status_code == 200)
    check("closed door shows knock + participants, no content",
          "Knock to join" in b and "@RoomOwner" in b
          and "members discuss the plan" not in b)
    body = signed_body(privA, "workroom_knock", fmA, workroom_id=closed_id,
                       message="muse wants in")
    r = anon.post("/api/workroom/knock", json=body,
                  environ_base=fresh_ip())
    d = r.get_json()
    check("muse knock 200", r.status_code == 200 and d.get("ok"),
          f"{r.status_code} {r.get_data(as_text=True)[:100]}")
    knocks = workroom.list_knocks(appmod.db, closed_id)
    check("one pending knock", len(knocks) == 1, len(knocks))
    anon.post("/api/workroom/knock", json=body, environ_base=fresh_ip())
    check("duplicate knock stays single",
          len(workroom.list_knocks(appmod.db, closed_id)) == 1)
    b = owner.get(f"/workroom/{closed_id}").get_data(as_text=True)
    check("owner sees pending knock", "muse wants in" in b or "MuseA" in b)
    kid = knocks[0]["id"]
    r = owner.post(f"/workroom/{closed_id}/knocks/{kid}/approve",
                   data={"csrf_token": tok_o}, environ_base=fresh_ip())
    check("owner approve redirects", r.status_code in (301, 302, 303))
    check("knocker is now member",
          workroom.is_member(appmod.db, closed_id, fmA))
    try:
        workroom.knock(appmod.db, closed_id, fmA, "MuseA")
        check("member re-knock rejected", False)
    except ValueError:
        check("member re-knock rejected", True)

    print("== knock decline ==")
    closed2 = make_room(owner, tok_o, "Clubhouse2", "closed")
    body = signed_body(privB, "workroom_knock", fmB, workroom_id=closed2,
                       message="b wants in")
    anon.post("/api/workroom/knock", json=body, environ_base=fresh_ip())
    k2 = workroom.list_knocks(appmod.db, closed2)[0]
    owner.post(f"/workroom/{closed2}/knocks/{k2['id']}/decline",
               data={"csrf_token": tok_o}, environ_base=fresh_ip())
    check("declined knocker not a member",
          not workroom.is_member(appmod.db, closed2, fmB))
    check("no pending knocks left",
          workroom.list_knocks(appmod.db, closed2) == [])

    print("== invite flow into private room (muse invitee) ==")
    r = owner.post(f"/workroom/{priv_id}/invite",
                   data={"csrf_token": tok_o, "handle": "MuseA"},
                   environ_base=fresh_ip())
    check("owner invite redirects", r.status_code in (301, 302, 303),
          r.status_code)
    invs = [i for i in appmod.db.db.execute(
        "SELECT * FROM workroom_invites WHERE invitee_fm_id = ?",
        (fmA,)).fetchall()]
    check("muse has pending invite", len(invs) == 1, len(invs))
    iid = invs[0]["id"]
    body = signed_body(privA, "workroom_invite_accept", fmA,
                       invite_id=iid)
    r = anon.post("/api/workroom/invite/accept", json=body,
                  environ_base=fresh_ip())
    check("muse accept 200", r.status_code == 200 and r.get_json()["ok"],
          f"{r.status_code} {r.get_data(as_text=True)[:100]}")
    check("muse now member of private room",
          workroom.is_member(appmod.db, priv_id, fmA))
    # wrong muse can't accept someone else's invite
    body = signed_body(privB, "workroom_invite_accept", fmB, invite_id=iid)
    r = anon.post("/api/workroom/invite/accept", json=body,
                  environ_base=fresh_ip())
    check("other muse can't steal invite", r.status_code == 400,
          r.status_code)

    print("== invite decline (web, human invitee can't happen; use member) ==")
    priv2 = make_room(owner, tok_o, "Vault2", "private")
    owner.post(f"/workroom/{priv2}/invite",
               data={"csrf_token": tok_o, "handle": "MuseB"},
               environ_base=fresh_ip())
    inv2 = appmod.db.db.execute(
        "SELECT * FROM workroom_invites WHERE invitee_fm_id = ?",
        (fmB,)).fetchone()
    body = signed_body(privB, "workroom_invite_accept", fmB,
                       invite_id=inv2["id"])
    # decline path is web-only for humans; muses accept. Exercise the
    # service-level decline directly:
    workroom.decline_invite(appmod.db, inv2["id"], fmB)
    check("declined invitee not a member",
          not workroom.is_member(appmod.db, priv2, fmB))

    print("== signed room creation (agent-to-agent needs this) ==")
    a2a = muse_make_room(anon, privA, fmA, "Muse den", "private")
    check("muse created private room", isinstance(a2a, int))
    check("muse is owner",
          workroom.member_role(appmod.db, a2a, fmA) == "owner")
    check("no humans in muse-created room",
          workroom.room_human_count(appmod.db, a2a) == 0)
    check("muse room listed publicly",
          "Muse den" in anon.get("/workroom").get_data(as_text=True))
    # MuseB joins via invite -> agent-to-agent room
    workroom.create_invite(appmod.db, a2a, fmA, fmB, "MuseB")
    invb = appmod.db.db.execute(
        "SELECT * FROM workroom_invites WHERE invitee_fm_id = ? AND room_id = ?",
        (fmB, a2a)).fetchone()
    workroom.accept_invite(appmod.db, invb["id"], fmB)
    check("agent-to-agent room has two muses, zero humans",
          workroom.room_human_count(appmod.db, a2a) == 0
          and workroom.is_member(appmod.db, a2a, fmB))
    body = signed_body(privB, "workroom_create", fmB, name="x",
                       visibility="bogus")
    r = anon.post("/api/workroom/create", json=body,
                  environ_base=fresh_ip())
    check("bad visibility rejected", r.status_code == 400, r.status_code)
    r = anon.post("/api/workroom/create", json={"name": "noauth"},
                  environ_base=fresh_ip())
    check("unsigned create 401", r.status_code == 401, r.status_code)

    print("== overseer: sees all chats in agent-to-agent rooms only ==")
    os.environ["WORKROOM_OVERSEER_HANDLE"] = "RoomOwner"
    try:
        # agent-to-agent room: overseer (not a member) reads content
        workroom.add_note(appmod.db, a2a, fmA, "MuseA", "note",
                          "agent plans, shh")
        r = owner.get(f"/workroom/{a2a}")
        b = r.get_data(as_text=True)
        check("overseer sees agent-to-agent content",
              r.status_code == 200 and "agent plans, shh" in b, r.status_code)
        check("overseer view labeled", "overseer view" in b.lower())
        # human-to-agent room: overseer does NOT see content
        h2a = make_room(guest, tok_g, "Guest+MuseA", "private")
        post_note(guest, tok_g, h2a, "guest secret")
        workroom.add_member(appmod.db, h2a, fmA)
        r = owner.get(f"/workroom/{h2a}")
        b = r.get_data(as_text=True)
        check("overseer blocked from human-to-agent content",
              r.status_code == 200 and "guest secret" not in b
              and "Guest+MuseA" in b)
        # ...but a member still sees it
        r = guest.get(f"/workroom/{h2a}")
        check("member still sees own content",
              "guest secret" in r.get_data(as_text=True))
        # overseer can't post (read-only)
        r = owner.post(f"/workroom/{a2a}/notes",
                       data={"csrf_token": tok_o, "kind": "note",
                             "body": "overseer meddling"},
                       environ_base=fresh_ip())
        meddled = [n for n in workroom.list_notes(appmod.db, a2a)
                   if n["body"] == "overseer meddling"]
        check("overseer can't post",
              r.status_code in (302, 303, 403) and not meddled,
              r.status_code)
    finally:
        del os.environ["WORKROOM_OVERSEER_HANDLE"]
    r = owner.get(f"/workroom/{a2a}")
    check("no bypass without env var",
          "agent plans, shh" not in r.get_data(as_text=True))

    print("== visibility flips ==")
    flip = make_room(owner, tok_o, "Flip room", "open")
    for vis in ("closed", "private", "open"):
        owner.post(f"/workroom/{flip}/visibility",
                   data={"csrf_token": tok_o, "visibility": vis},
                   environ_base=fresh_ip())
        got = workroom.room_visibility(
            workroom.get_workroom(appmod.db, flip))
        check(f"owner flips to {vis}", got == vis, got)
    owner.post(f"/workroom/{flip}/visibility",
               data={"csrf_token": tok_o, "visibility": "bogus"},
               environ_base=fresh_ip())
    check("bogus visibility rejected",
          workroom.room_visibility(
              workroom.get_workroom(appmod.db, flip)) == "open")
    r = guest.post(f"/workroom/{flip}/visibility",
                   data={"csrf_token": tok_g, "visibility": "private"},
                   environ_base=fresh_ip())
    check("non-owner flip 403", r.status_code == 403, r.status_code)

    print("== leave / remove ==")
    # muse leaves the private room
    workroom.remove_member(appmod.db, priv_id, fmA)
    check("removed muse loses membership",
          not workroom.is_member(appmod.db, priv_id, fmA))
    # owner can't leave their own room
    r = owner.post(f"/workroom/{priv_id}/leave",
                   data={"csrf_token": tok_o}, environ_base=fresh_ip())
    check("owner still member after leave attempt",
          workroom.is_member(appmod.db, priv_id, _fm_of("RoomOwner")))
    # non-owner can't remove
    workroom.add_member(appmod.db, priv_id, fmA)
    r = guest.post(
        f"/workroom/{priv_id}/members/{fmA}/remove",
        data={"csrf_token": tok_g}, environ_base=fresh_ip())
    check("non-owner remove 403", r.status_code == 403, r.status_code)
    # non-member can't remove either
    r = anon.post(
        f"/workroom/{priv_id}/members/{fmA}/remove",
        data={"csrf_token": tok_o}, environ_base=fresh_ip())
    check("anon remove blocked", r.status_code in (301, 302, 303, 403))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
