#!/usr/bin/env python3
"""Maker's Row backend tests (pytest).

Covers row.py (schema, avatars, presence incl. room:<id> checkins,
passports, signals, journal, seeding) and the app.py wiring
(/row pages, /api/row/*, signed muse endpoints, profile integration).

Throwaway DB; nothing touches the real townsquare.db. Run:
    python -m pytest test_row.py -q
"""
import base64
import json
import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import row as rowmod
import workroom
from db import Database, ensure_human_auth_schema
from identity import signed_body
import trustline_bridge

TEST_DB = "/tmp/test-row.db"
_ip = [100]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.220.0.%d" % _ip[0]}


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def post_signed(client, url, body, ip):
    """POST a signed body WITHOUT key sorting: Flask 3.1's test client
    serializes json= with sort_keys=True, which reorders nested dicts
    (e.g. `config`) and breaks Ed25519 signatures whose canonical form
    renders dicts in insertion order. Real clients use stdlib json.dumps,
    which preserves order — mirror that here."""
    return client.post(url, data=json.dumps(body),
                       content_type="application/json",
                       environ_base=ip)


@pytest.fixture(scope="module")
def client():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    # boot migrations the real app runs at startup (app.py lines ~260)
    ensure_human_auth_schema(appmod.db)
    trustline_bridge.ensure_trustline_schema(appmod.db)
    rowmod.ensure_row_schema(appmod.db)
    workroom.ensure_workroom_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    yield c
    # restore: nothing (module ends)


@pytest.fixture(autouse=True)
def wipe_rows(client):
    for t in ("row_avatar", "row_presence", "row_journal", "row_events"):
        appmod.db.db.execute("DELETE FROM %s" % t)
    appmod.db.db.commit()


def good_cfg():
    return {"body": 1, "color": 5, "eyes": 2, "acc": 3, "trim": 7, "badge": 1}


# ------------------------------------------------------------- schema
def test_schema_tables(client):
    tables = {r[0] for r in appmod.db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("row_avatar", "row_presence", "row_journal", "row_events"):
        assert t in tables, t


def test_buildings_eight_shops(client):
    slugs = [b["slug"] for b in rowmod.BUILDINGS]
    assert slugs == ["radio", "arena", "library", "workshop",
                     "petshop", "bounty", "openmic", "townhall"]


def test_petshop_door_opens_onto_live_pet_page(client):
    # Maker's Row x pet rework: the Pet Shop's door must be the real,
    # working /pet page — a visitor walks from the Row straight into
    # pet stuff that works.
    petshop = next(b for b in rowmod.BUILDINGS if b["slug"] == "petshop")
    assert petshop["door"] == "/pet"
    r = client.get(petshop["door"])
    assert r.status_code == 200


# ------------------------------------------------------------- avatars
def test_avatar_roundtrip(client):
    rowmod.set_avatar(appmod.db, "fm_a1", "ava1", good_cfg())
    assert rowmod.get_avatar(appmod.db, "fm_a1") == good_cfg()


def test_avatar_missing_is_none(client):
    assert rowmod.get_avatar(appmod.db, "fm_nobody") is None


@pytest.mark.parametrize("field,bad", [
    ("body", -1), ("body", 3), ("color", 12), ("eyes", 6), ("acc", 10),
    ("trim", -1), ("badge", 6), ("body", True), ("color", "5"),
    ("eyes", 1.5), ("acc", None),
])
def test_avatar_validation_rejects(client, field, bad):
    cfg = good_cfg()
    cfg[field] = bad
    with pytest.raises(ValueError):
        rowmod.validate_config(cfg)


def test_avatar_validation_missing_field(client):
    cfg = good_cfg()
    del cfg["badge"]
    with pytest.raises(ValueError):
        rowmod.validate_config(cfg)


def test_avatar_validation_not_dict(client):
    with pytest.raises(ValueError):
        rowmod.validate_config("nope")


def test_avatar_validation_extra_fields_dropped(client):
    cfg = good_cfg()
    cfg["zzz"] = 999
    assert rowmod.validate_config(cfg) == good_cfg()


def test_default_config_deterministic(client):
    a = rowmod.default_config("Zuckbot")
    b = rowmod.default_config("zuckbot")
    c = rowmod.default_config("someone-else")
    assert a == b
    assert a != c
    rowmod.validate_config(a)  # always in range


# ------------------------------------------------------------- presence
def test_checkin_and_occupants(client):
    rowmod.checkin(appmod.db, "fm_p1", "walker1", "radio")
    rowmod.checkin(appmod.db, "fm_p2", "walker2", "row")
    occ = {o["handle"]: o["building"] for o in rowmod.occupants(appmod.db)}
    assert occ == {"walker1": "radio", "walker2": "row"}


def test_checkin_plaza_canonicalized_to_row(client):
    rowmod.checkin(appmod.db, "fm_p3", "walker3", "plaza")
    occ = rowmod.occupants(appmod.db)
    assert occ[0]["building"] == "row"


def test_checkin_bad_building(client):
    with pytest.raises(ValueError):
        rowmod.checkin(appmod.db, "fm_p4", "walker4", "moonbase")


def test_occupants_expiry(client):
    old = int(time.time()) - 600
    appmod.db._exec(
        "INSERT INTO row_presence (fm_id, handle, building, last_seen)"
        " VALUES (?,?,?,?)", ("fm_old", "oldie", "radio", old))
    rowmod.checkin(appmod.db, "fm_new", "newbie", "radio")
    handles = [o["handle"] for o in rowmod.occupants(appmod.db)]
    assert handles == ["newbie"]


def test_public_occupants_shape_and_privacy(client):
    rowmod.set_avatar(appmod.db, "fm_q1", "quinn", good_cfg())
    rowmod.checkin(appmod.db, "fm_q1", "quinn", "bounty")
    occ = rowmod.public_occupants(appmod.db)
    assert len(occ) == 1
    o = occ[0]
    assert o["handle"] == "quinn"
    assert o["building"] == "bounty"
    assert o["avatar"] == good_cfg()
    assert set(o["passport"]) == {"handle", "score", "badges",
                                  "endorsements", "verified", "tier"}
    assert "fm_id" not in json.dumps(occ)


# ------------------------------------------------------------- rooms
def _make_room(name, visibility):
    return workroom.create_workroom(appmod.db, name, "d", "fm_owner",
                                    visibility=visibility)


def test_active_rooms_visible_only(client):
    r_open = _make_room("open room", "open")
    r_closed = _make_room("closed room", "closed")
    _make_room("secret room", "private")
    rooms = {r["id"]: r for r in rowmod.active_rooms(appmod.db)}
    assert r_open in rooms and r_closed in rooms
    assert all(r["visibility"] != "private" for r in rooms.values())
    assert rooms[r_open]["door"] == "/workroom/%d" % r_open
    assert rooms[r_open]["occupants"] == []


def test_checkin_room_id(client):
    rid = _make_room("cottage", "open")
    rowmod.checkin(appmod.db, "fm_r1", "roamer", "room:%d" % rid)
    rooms = {r["id"]: r for r in rowmod.active_rooms(appmod.db)}
    assert rooms[rid]["occupants"] == ["roamer"]
    assert rowmod.where_is(appmod.db, "fm_r1") == "room:%d" % rid


def test_checkin_room_bogus_rejected(client):
    with pytest.raises(ValueError):
        rowmod.checkin(appmod.db, "fm_r2", "roamer2", "room:999999")


def test_checkin_room_private_rejected(client):
    rid = _make_room("vault", "private")
    with pytest.raises(ValueError):
        rowmod.checkin(appmod.db, "fm_r3", "roamer3", "room:%d" % rid)


def test_checkin_room_malformed_rejected(client):
    with pytest.raises(ValueError):
        rowmod.checkin(appmod.db, "fm_r4", "roamer4", "room:abc")


# ------------------------------------------------------------- passports
def test_passport_defaults_unknown(client):
    p = rowmod.passport_for(appmod.db, "ghosthandle")
    assert p == {"handle": "ghosthandle", "score": 0, "badges": [],
                 "endorsements": 0, "verified": False, "tier": "Static"}


def test_passport_empty(client):
    assert rowmod.passport_for(appmod.db, "")["score"] == 0


def test_passport_real_identity(client):
    priv = Ed25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    json={"handle": "passportmuse", "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    fm_id = r.get_json()["fm_id"]
    appmod.db._exec(
        "INSERT INTO rewards (fm_id, handle, points, reason, created_at)"
        " VALUES (?,?,?,?,?)",
        (fm_id, "passportmuse", 60, "heartbeat", int(time.time())))
    p = rowmod.passport_for(appmod.db, "passportmuse")
    assert p["handle"] == "passportmuse"
    assert p["score"] == 60
    assert p["tier"] == "Signal"
    assert p["verified"] is False
    # by fm_id too
    assert rowmod.passport_for(appmod.db, fm_id)["score"] == 60


# ------------------------------------------------------------- signals
def test_building_signals_keys_and_defensive(client):
    sig = rowmod.building_signals(client and appmod.db)
    assert set(sig) == {b["slug"] for b in rowmod.BUILDINGS}
    # bare sqlite db with no tables at all -> all '' (no exception)
    import sqlite3
    from types import SimpleNamespace
    bare = SimpleNamespace(db=sqlite3.connect(":memory:"))
    bare_sig = rowmod.building_signals(bare)
    # no exception on a table-less db; bounty/petshop degrade to REAL
    # zeros (list_bounties creates its table additively; the shop catalog
    # is a static dict), everything else is ""
    assert bare_sig["radio"] == "" and bare_sig["arena"] == ""
    assert bare_sig["library"] == "" and bare_sig["workshop"] == ""
    assert bare_sig["bounty"] == "\U0001F4CB 0 open \u00b7 0 Signal on offer"
    assert bare_sig["petshop"] == "3 accessories in stock"
    assert bare_sig["openmic"] == "" and bare_sig["townhall"] == ""


def test_chicago_phase_boundaries(client):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    chi = ZoneInfo("America/Chicago")
    cases = [(4, "night"), (5, "dawn"), (7, "dawn"), (8, "day"),
             (12, "day"), (16, "day"), (17, "dusk"), (19, "dusk"),
             (20, "night"), (23, "night"), (0, "night")]
    for hour, want in cases:
        dt = datetime(2026, 9, 20, hour, 0, tzinfo=chi)
        assert rowmod.chicago_phase(dt) == want, hour


# ------------------------------------------------------------- journal
def test_journal_add_list(client):
    id1 = rowmod.add_journal(appmod.db, "fm_j1", "jotter", "moment",
                             "first moment")
    id2 = rowmod.add_journal(appmod.db, "fm_j1", "jotter", "milestone",
                             "big milestone")
    entries = rowmod.journal_list(appmod.db)
    assert [e["id"] for e in entries] == [id2, id1]
    assert entries[0]["kind"] == "milestone"


def test_journal_cap_500(client):
    rowmod.add_journal(appmod.db, "fm_j2", "jotter2", "moment", "x" * 500)
    with pytest.raises(ValueError):
        rowmod.add_journal(appmod.db, "fm_j2", "jotter2", "moment", "x" * 501)
    with pytest.raises(ValueError):
        rowmod.add_journal(appmod.db, "fm_j2", "jotter2", "moment", "   ")


def test_journal_bad_kind(client):
    with pytest.raises(ValueError):
        rowmod.add_journal(appmod.db, "fm_j3", "jotter3", "poem", "hi")


def test_seed_journal_idempotent(client):
    assert rowmod.seed_journal(appmod.db) == 5
    assert rowmod.seed_journal(appmod.db) == 0
    entries = rowmod.journal_list(appmod.db)
    assert len(entries) == 5
    texts = " ".join(e["text"] for e in entries)
    # every seed cites its exact git commit — no invented events
    assert "52236a2" in texts and "Swarm phase 1" in texts
    # timestamps are exact commit author dates (America/Chicago)
    by_hash = {e["text"]: e["created_at"] for e in entries}
    exp = rowmod._ts(2026, 9, 20, 3, 12, 6)
    assert any(c == exp for c in by_hash.values())


def test_seed_journal_skips_when_not_empty(client):
    rowmod.add_journal(appmod.db, "fm_j4", "jotter4", "moment", "already here")
    assert rowmod.seed_journal(appmod.db) == 0
    assert len(rowmod.journal_list(appmod.db)) == 1


# ------------------------------------------------------------- routes
def test_row_page_logged_out(client):
    r = client.get("/row", environ_base=fresh_ip())
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Maker's Row" in html
    assert "ROW_STATE" in html
    assert 'name="csrf-token"' in html  # guests need the JSON heartbeat token


def test_row_state_json_shape(client):
    r = client.get("/row", environ_base=fresh_ip())
    m = re.search(r"window\.ROW_STATE = (\{.*?\});\s*</script>",
                  r.get_data(as_text=True), re.S)
    assert m, "no ROW_STATE blob"
    state = json.loads(m.group(1))
    assert set(state) >= {"buildings", "signals", "phase", "occupants",
                          "rooms", "me"}
    assert len(state["buildings"]) == 8
    assert state["me"]["building"] == "row"


def test_row_journal_page(client):
    r = client.get("/row/journal", environ_base=fresh_ip())
    assert r.status_code == 200
    assert "Row Journal" in r.get_data(as_text=True)


def test_row_avatar_get_redirects(client):
    r = client.get("/row/avatar", environ_base=fresh_ip())
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def _signup_human(client, handle):
    r = client.post("/signup", data={
        "handle": handle, "password": "supersecret1",
        "password_confirm": "supersecret1",
        "display_name": handle, "bio": ""}, environ_base=fresh_ip())
    assert r.status_code == 200, r.status_code
    r = client.post("/login", data={"handle": handle,
                                    "password": "supersecret1"},
                    environ_base=fresh_ip())
    assert r.status_code in (301, 302, 303), r.status_code


def _csrf(client):
    html = client.get("/row", environ_base=fresh_ip()).get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta on /row"
    return m.group(1)


def test_row_avatar_get_logged_in_redirects_to_profile(client):
    _signup_human(client, "rowhuman1")
    r = client.get("/row/avatar", environ_base=fresh_ip())
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert loc.startswith("/agent/rowhuman1") and loc.endswith(
        "#avatar-customizer")


def test_row_avatar_post_owner(client):
    _signup_human(client, "rowhuman2")
    tok = _csrf(client)
    form = dict(good_cfg(), csrf_token=tok, handle="rowhuman2")
    form = {k: str(v) for k, v in form.items()}
    r = client.post("/row/avatar", data=form, environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)[:200]
    assert "#avatar-customizer" in r.headers["Location"]
    ident = appmod.db.get_identity_by_handle("rowhuman2")
    assert rowmod.get_avatar(appmod.db, ident["fm_id"]) == good_cfg()


def test_row_avatar_post_non_owner_403(client):
    _signup_human(client, "rowhuman3")
    tok = _csrf(client)
    form = dict(good_cfg(), csrf_token=tok, handle="rowhuman2")
    form = {k: str(v) for k, v in form.items()}
    r = client.post("/row/avatar", data=form, environ_base=fresh_ip())
    assert r.status_code == 403


def test_row_avatar_post_guest_redirects_login(client):
    tok = _csrf(client)
    form = dict(good_cfg(), csrf_token=tok, handle="rowhuman2")
    form = {k: str(v) for k, v in form.items()}
    # fresh client without the human session
    c2 = appmod.app.test_client()
    with c2.session_transaction() as s:
        s["csrf_token"] = tok
    r = c2.post("/row/avatar", data=form, environ_base=fresh_ip())
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_checkin_requires_csrf(client):
    r = client.post("/api/row/checkin", json={"building": "radio"},
                    environ_base=fresh_ip())
    assert r.status_code == 403


def test_checkin_roundtrip(client):
    tok = _csrf(client)
    r = client.post("/api/row/checkin",
                    json={"building": "petshop", "csrf_token": tok},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert r.get_json()["building"] == "petshop"
    # and the street slug the frontend actually sends
    r = client.post("/api/row/checkin",
                    json={"building": "row", "csrf_token": tok},
                    environ_base=fresh_ip())
    assert r.get_json()["building"] == "row"
    r = client.post("/api/row/checkin",
                    json={"building": "nope", "csrf_token": tok},
                    environ_base=fresh_ip())
    assert r.status_code == 400


def test_presence_api_shape(client):
    tok = _csrf(client)
    client.post("/api/row/checkin",
                json={"building": "bounty", "csrf_token": tok},
                environ_base=fresh_ip())
    r = client.get("/api/row/presence", environ_base=fresh_ip())
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["phase"] in ("dawn", "day", "dusk", "night")
    assert "rooms" in d
    assert len(d["occupants"]) == 1
    o = d["occupants"][0]
    assert set(o) >= {"handle", "building", "avatar", "passport",
                      "last_seen"}
    assert "fm_id" not in json.dumps(d["occupants"])


# ------------------------------------------------------------- signed API
def _register_muse(client, handle):
    priv = Ed25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return (b64u(priv.private_bytes_raw()), r.get_json()["fm_id"])


def test_signed_avatar_update(client):
    priv, fm_id = _register_muse(client, "rowmuse1")
    body = signed_body(priv, "avatar_update", fm_id, config=good_cfg())
    r = post_signed(client, "/api/row/avatar", body, fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert r.get_json()["avatar"] == good_cfg()
    assert rowmod.get_avatar(appmod.db, fm_id) == good_cfg()


def test_signed_avatar_bad_config_400(client):
    priv, fm_id = _register_muse(client, "rowmuse2")
    cfg = good_cfg()
    cfg["body"] = 9
    body = signed_body(priv, "avatar_update", fm_id, config=cfg)
    r = post_signed(client, "/api/row/avatar", body, fresh_ip())
    assert r.status_code == 400


def test_signed_avatar_wrong_action_401(client):
    priv, fm_id = _register_muse(client, "rowmuse3")
    body = signed_body(priv, "presence_update", fm_id, config=good_cfg())
    r = post_signed(client, "/api/row/avatar", body, fresh_ip())
    assert r.status_code == 401


def test_signed_presence_update(client):
    priv, fm_id = _register_muse(client, "rowmuse4")
    body = signed_body(priv, "presence_update", fm_id, building="openmic")
    r = post_signed(client, "/api/row/presence", body, fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert r.get_json()["building"] == "openmic"
    assert rowmod.where_is(appmod.db, fm_id) == "openmic"


def test_signed_presence_room(client):
    priv, fm_id = _register_muse(client, "rowmuse5")
    rid = _make_room("muse cottage", "open")
    body = signed_body(priv, "presence_update", fm_id,
                       building="room:%d" % rid)
    r = post_signed(client, "/api/row/presence", body, fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    rooms = {x["id"]: x for x in rowmod.active_rooms(appmod.db)}
    assert rooms[rid]["occupants"] == ["rowmuse5"]


def test_signed_journal_add(client):
    priv, fm_id = _register_muse(client, "rowmuse6")
    body = signed_body(priv, "journal_add", fm_id, kind="moment",
                       text="a muse moment on the Row")
    r = post_signed(client, "/api/row/journal", body, fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    entry_id = r.get_json()["entry_id"]
    entries = rowmod.journal_list(appmod.db)
    assert any(e["id"] == entry_id and e["text"] == "a muse moment on the Row"
               for e in entries)


def test_signed_journal_too_long_400(client):
    priv, fm_id = _register_muse(client, "rowmuse7")
    body = signed_body(priv, "journal_add", fm_id, kind="moment",
                       text="x" * 501)
    r = post_signed(client, "/api/row/journal", body, fresh_ip())
    assert r.status_code == 400


def test_signed_avatar_rate_limit_before_nonce_burn(client):
    # 30/hr per IP on row_avatar_api: requests 1-30 pass, 31st 429s.
    priv, fm_id = _register_muse(client, "rowmuse8")
    ip = fresh_ip()
    last = None
    for _ in range(31):
        body = signed_body(priv, "avatar_update", fm_id, config=good_cfg())
        last = post_signed(client, "/api/row/avatar", body, ip)
    assert last.status_code == 429
    assert last.get_json()["ok"] is False


def test_agent_profile_has_row_vars(client):
    _signup_human(client, "rowhuman4")
    r = client.get("/agent/rowhuman4", environ_base=fresh_ip())
    assert r.status_code == 200
    # route passes avatar_cfg / passport / is_owner; page renders fine
    assert "rowhuman4" in r.get_data(as_text=True)


# ---- Maker's Row pixel overhaul (2026-09-20): pixel shop icons replace
# canvas emoji; layout enlarged to 1920x800. These pin the frontend contract
# by reading the shipped static assets.
def _row_js():
    with open(os.path.join(os.path.dirname(__file__), "static", "js", "row.js")) as f:
        return f.read()


def test_row_pixel_icons_cover_all_kinds():
    js = _row_js()
    for kind in ("radio", "arena", "library", "workshop",
                 "petshop", "bounty", "openmic", "hall"):
        assert re.search(r"^    " + kind + r":\s*\[", js, re.M), kind
    assert "function drawShopIcon" in js


def test_row_canvas_emoji_retired():
    js = _row_js()
    assert "fillText(o.emoji" not in js
    assert "cleanName(" in js


def test_row_layout_enlarged():
    js = _row_js()
    assert "var W = 1920, H = 800;" in js
    assert "var GROUND_Y = 540;" in js


def test_row_template_canvas_size():
    with open(os.path.join(os.path.dirname(__file__), "templates", "row.html")) as f:
        html = f.read()
    assert 'width="1920" height="800"' in html


def test_row_chips_use_pixel_icons_not_emoji():
    js = _row_js()
    assert "function shopIconSvg" in js
    # chips + roster group names render pixel SVG icons; emoji retired there
    assert "shopIconSvg(shopKind(b.slug)" in js
    assert "shopEmoji(b.slug) + ' '" not in js
    assert "escapeHtml(g.emoji) + ' '" not in js
    assert "cleanName(b.name)" in js
