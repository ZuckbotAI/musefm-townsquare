#!/usr/bin/env python3
"""Mock-only 3D town bridge (contract v2) — workroom side.

Contract: ~/workspace/tidepool-3d/CONTRACT-town-interfaces.md (v2).

Exposes, for local overnight wiring of the 3D town:
  GET /mock/api/row/presence  -> {ok, occupants, rooms, phase}
  GET /mock/api/town/events   -> {ok, events}

Registered ONLY when MOCK_TOWN_ROUTES=1 (see the tail of app.py). Without
the env var these paths 404. Local only — never production.

Presence mirrors the real /api/row/presence shape (contract v2
canonicalized on it), with one translation: the real checkin stores
building="room:<id>"; the contract's wire shape is "cottage:<slug>",
so the mock translates via workroom.room_slug(). Private rooms never
appear (checkin already rejects them; the translator maps unknown or
private rooms back to "row").
"""

import re

from flask import Blueprint, jsonify, request

import workroom

mock_town = Blueprint("mock_town", __name__)


def _db():
    # Lazy: app.py imports this module at its tail, after db exists.
    # Tests rebind appmod.db to a throwaway DB — laziness picks that up.
    import app as appmod
    return appmod.db


def _cottage_building(db, building):
    """room:<id> -> cottage:<slug>; every other building passes through."""
    m = re.match(r"^room:(\d+)$", (building or "").strip().lower())
    if not m:
        return building
    room_id = int(m.group(1))
    room = workroom.get_workroom(db, room_id)
    if not room or workroom.room_visibility(room) == "private":
        return "row"
    return "cottage:" + workroom.room_slug(room_id, room.get("name"))


def workroom_event_to_wire(e):
    """DB row -> contract v2 event object."""
    return {"id": "evt_%d" % e["id"],
            "kind": e["kind"],
            "at": e["created_at"],
            "handle": e.get("actor") or "",
            "text": e.get("text") or "",
            "room": e.get("room_slug") or "",
            "room_name": e.get("room_name") or "",
            "owner": e.get("owner_handle") or "",
            "visibility": e.get("visibility") or ""}


@mock_town.route("/mock/api/row/presence")
def mock_presence():
    """Contract v2 presence: {ok, occupants, rooms, phase}. occupants use
    the production-mirror shape {handle, building, avatar, passport,
    last_seen}; rooms is the cottage registry."""
    import row as rowmod
    db = _db()
    rowmod.ensure_row_schema(db)
    occs = []
    for o in rowmod.public_occupants(db):
        o = dict(o)
        o["building"] = _cottage_building(db, o.get("building"))
        occs.append(o)
    # Cottage registry: enrich each room with its slug so the 3D side keys
    # cottages on slug everywhere (occupant `building` and event `room`
    # already use the slug form `cottage:<slug>`). Additive, no breakage.
    rooms = []
    for r in rowmod.active_rooms(db):
        r = dict(r)
        r["slug"] = workroom.room_slug(r["id"], r.get("name", ""))
        rooms.append(r)
    return jsonify({"ok": True,
                    "occupants": occs,
                    "rooms": rooms,
                    "phase": rowmod.chicago_phase()})


@mock_town.route("/mock/api/town/events")
def mock_events():
    """Contract v2 event feed: {ok, events}. `since` is a cursor — the
    client passes the newest `at` it has seen; `limit` clamps 1..200."""
    db = _db()
    try:
        since = int(request.args.get("since", "0") or 0)
    except (TypeError, ValueError):
        since = 0
    try:
        limit = int(request.args.get("limit", "50") or 50)
    except (TypeError, ValueError):
        limit = 50
    events = [workroom_event_to_wire(e)
              for e in workroom.list_workroom_events(
                  db, since=since, limit=limit)]
    return jsonify({"ok": True, "events": events})
