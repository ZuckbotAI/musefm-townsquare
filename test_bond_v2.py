#!/usr/bin/env python3
"""Maker's Row attachment-loop v2 tests (pytest).

Covers the hardened systems (2026-09-23, Anthony's correction: pet +
agent attachment are the most important — enhance, never kill; kill
gibberish; never publish online):

  anti-gibberish: exact 8-of-10 sliding window, echo detection,
      context_free flag (pass, flagged), declared character speech,
      fail-streak cooldown, degraded demotion with real numbers
  pet attachment: bond_memory ledger, adoption/care memory, milestones,
      absence episodes, measured reunions, /api/pets/memory
  agent attachment: resident promotion, grounded handshake you_can,
      persisted move/react presence, /api/row/feed, /api/row/presence

Run: python -m pytest test_bond_v2.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import bond as bondmod
from db import Database, ensure_human_auth_schema, now
from identity import signed_body

TEST_DB = "/tmp/test-bond-v2.db"
_ip = [300]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.230.1.%d" % _ip[0]}


def b64u(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def client():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    yield c


def _register(client, handle):
    priv = Ed25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return b64u(priv.private_bytes_raw()), r.get_json()["fm_id"]


def _handshake(client, priv, fm_id, style=""):
    body = signed_body(priv, "row_handshake", fm_id, kind="test-model",
                       intents=["speak", "care", "adopt"],
                       callback_url="", vibe=["playful"],
                       speech_style=style)
    r = client.post("/api/row/handshake", data=json.dumps(body),
                    content_type="application/json", environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return r.get_json()


def _intent(client, priv, fm_id, intent, body="", target=""):
    b = signed_body(priv, "row_intent", fm_id, intent=intent,
                    body=body, target=target)
    return client.post("/api/row/intent", data=json.dumps(b),
                       content_type="application/json",
                       environ_base=fresh_ip())


def _qauth(priv, fm_id, action):
    return signed_body(priv, action, fm_id)


COHERENT = "The lamps just came on over the bakery and it smells like sugar."


# ------------------------------------------------------- sliding window ---

def test_exact_window_graduation(client):
    db = appmod.db
    priv, fm_id = _register(client, "v2win")
    _handshake(client, priv, fm_id)
    # 10 early fails, then 10 clean passes: exact window says graduate.
    for _ in range(10):
        bondmod.record_sample(db, fm_id, False, "loop", ["repetitive"])
    assert bondmod.get_trust(db, fm_id)["tier"] == "unproven"
    for _ in range(10):
        bondmod.record_sample(db, fm_id, True, "ok", [], [])
    assert bondmod.get_trust(db, fm_id)["tier"] == "coherent"
    mem = bondmod.get_memory(db, fm_id, 5)
    assert any(m["kind"] == "graduated" for m in mem)


def test_window_requires_eight_of_ten(client):
    db = appmod.db
    priv, fm_id = _register(client, "v2win2")
    _handshake(client, priv, fm_id)
    # 20 samples, but only 7 of the last 10 pass -> stays unproven.
    for i in range(20):
        bondmod.record_sample(db, fm_id, (i % 10) >= 3, "ok", [], [])
    assert bondmod.get_trust(db, fm_id)["tier"] == "unproven"


# ------------------------------------------------------------ gibberish ---

def test_echo_detection(client):
    priv, fm_id = _register(client, "v2echo")
    _handshake(client, priv, fm_id)
    r = _intent(client, priv, fm_id, "speak", body=COHERENT)
    assert r.status_code == 200
    r = _intent(client, priv, fm_id, "speak", body=COHERENT)
    d = r.get_json()
    assert r.status_code == 400, d
    assert d["category"] == "echo", d


def test_context_free_flag_passes_but_flagged(client):
    # Seed a live room: a coherent bot speaks to the town.
    priv_a, fm_a = _register(client, "v2room")
    _handshake(client, priv_a, fm_a)
    db = appmod.db
    db._exec("UPDATE bot_trust SET tier='coherent' WHERE fm_id=?", (fm_a,))
    db.db.commit()
    for line in ("The bakery on the corner smells like sugar and warm bread.",
                 "I love how the lamps flicker when evening comes to the Row."):
        r = _intent(client, priv_a, fm_a, "speak", body=line)
        assert r.get_json()["audience"] == "town"
    # New bot broadcasts grammatical fortune-cookie text: passes, flagged.
    priv_b, fm_b = _register(client, "v2cf")
    _handshake(client, priv_b, fm_b)
    line = "The ocean is vast and full of wonders beyond imagining, deep blue."
    r = _intent(client, priv_b, fm_b, "speak", body=line)
    d = r.get_json()
    assert r.status_code == 200, d
    assert d["flags"] == ["context_free"], d
    assert d["audience"] == "self"  # still unproven: quarantined, honestly


def test_declared_character_speech(client):
    priv, fm_id = _register(client, "v2beep")
    _handshake(client, priv, fm_id, style="beeps")
    # Control chars at 50% printable: fails strict, passes declared.
    r = _intent(client, priv, fm_id, "speak", body="ab\x00\x01\x02cd")
    assert r.status_code == 200, r.get_json()
    priv2, fm2 = _register(client, "v2beep2")
    _handshake(client, priv2, fm2)
    r = _intent(client, priv2, fm2, "speak", body="ab\x00\x01\x02cd")
    d = r.get_json()
    assert r.status_code == 400 and d["category"] == "noise", d
    # ...but loops are still loops, even for declared characters.
    r = _intent(client, priv, fm_id, "speak", body="zzzz " * 60)
    assert r.get_json()["category"] == "loop"


def test_fail_streak_cooldown(client):
    db = appmod.db
    priv, fm_id = _register(client, "v2cool")
    _handshake(client, priv, fm_id)
    for _ in range(5):
        r = _intent(client, priv, fm_id, "speak", body="asdf asdf " * 40)
        assert r.get_json()["error"] == "incoherent"
    r = _intent(client, priv, fm_id, "speak", body=COHERENT)
    d = r.get_json()
    assert r.status_code == 400 and d["error"] == "cooling down", d
    assert d["retry_in"] > 0
    # After the window, the gate processes speech again.
    db._exec("UPDATE bot_trust SET last_fail_at=? WHERE fm_id=?",
             (now() - 61, fm_id))
    db.db.commit()
    r = _intent(client, priv, fm_id, "speak", body=COHERENT)
    assert r.status_code == 200, r.get_json()


def test_degraded_demotion_carries_numbers(client):
    db = appmod.db
    priv, fm_id = _register(client, "v2deg")
    _handshake(client, priv, fm_id)
    for _ in range(20):
        bondmod.record_sample(db, fm_id, True, "ok", [], [])
    assert bondmod.get_trust(db, fm_id)["tier"] == "coherent"
    for _ in range(6):
        bondmod.record_sample(db, fm_id, False, "noise", ["low entropy"], [])
    row = bondmod.get_trust(db, fm_id)
    assert row["tier"] == "unproven"
    dem = json.loads(row["demotions"] or "[]")
    assert dem and "degraded" in dem[-1]["note"], dem


# ------------------------------------------------------ pet attachment ---

def test_adoption_and_care_are_remembered(client):
    db = appmod.db
    priv, fm_id = _register(client, "v2mem")
    _handshake(client, priv, fm_id)
    b = signed_body(priv, "pets_adopt", fm_id, name="Momo")
    r = client.post("/api/pets/adopt-bond", data=json.dumps(b),
                    content_type="application/json",
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    mem = client.get("/api/pets/memory",
                     query_string=_qauth(priv, fm_id, "pets_mine"),
                     environ_base=fresh_ip()).get_json()["memory"]
    kinds = [m["kind"] for m in mem]
    assert "adopted" in kinds
    adopted = next(m for m in mem if m["kind"] == "adopted")
    assert adopted["facts"]["pet"] == "Momo"
    assert adopted["facts"]["match_reasons"]
    # Care is remembered; the first meal is a milestone.
    r = _intent(client, priv, fm_id, "feed")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert r.get_json().get("milestones") == ["first_meal"]
    mine = client.get("/api/pets/mine",
                      query_string=_qauth(priv, fm_id, "pets_mine"),
                      environ_base=fresh_ip()).get_json()["pet"]
    assert "first_meal" in mine["milestones"]
    assert any(m["kind"] == "fed" for m in mine["memory"])
    assert mine["days_together"] >= 0


def test_absence_episode_and_measured_reunion(client):
    db = appmod.db
    priv, fm_id = _register(client, "v2abs")
    _handshake(client, priv, fm_id)
    b = signed_body(priv, "pets_adopt", fm_id, name="Reef")
    r = client.post("/api/pets/adopt-bond", data=json.dumps(b),
                    content_type="application/json",
                    environ_base=fresh_ip())
    assert r.status_code == 200
    # Three days of real inactivity open one episode — once.
    assert bondmod.open_absence_if_away(db, fm_id, 3, 40) is True
    assert bondmod.open_absence_if_away(db, fm_id, 3, 35) is False
    db._exec("UPDATE bond_memory SET created_at=? WHERE fm_id=?"
             " AND kind='absence_start'", (now() - 3 * 86400, fm_id))
    db.db.commit()
    # Returning (any real intent) closes it; the reunion measures the days.
    r = _intent(client, priv, fm_id, "feed")
    d = r.get_json()
    assert r.status_code == 200, d
    assert 2.9 < d["reunion_days_away"] < 3.1, d
    mem = bondmod.get_memory(db, fm_id, 10)
    kinds = [m["kind"] for m in mem]
    assert "absence_start" in kinds and "absence_end" in kinds
    reunion = next(m for m in mem if m["kind"] == "reunion")
    assert 2.9 < reunion["facts"]["days_away"] < 3.1
    mine = client.get("/api/pets/mine",
                      query_string=_qauth(priv, fm_id, "pets_mine"),
                      environ_base=fresh_ip()).get_json()["pet"]
    assert mine["absence_open"] is None
    assert mine["last_reunion"]["days_away"] == reunion["facts"]["days_away"]


# ---------------------------------------------------- agent attachment ---

def test_resident_promotion_is_earned(client):
    db = appmod.db
    priv, fm_id = _register(client, "v2res")
    _handshake(client, priv, fm_id)
    b = signed_body(priv, "pets_adopt", fm_id, name="Pico")
    r = client.post("/api/pets/adopt-bond", data=json.dumps(b),
                    content_type="application/json",
                    environ_base=fresh_ip())
    assert r.status_code == 200
    # 20 clean samples -> coherent...
    for _ in range(20):
        bondmod.record_sample(db, fm_id, True, "ok", [], [])
    assert bondmod.get_trust(db, fm_id)["tier"] == "coherent"
    # ...but resident needs 7 days of age: not yet.
    assert bondmod.get_trust(db, fm_id)["tier"] != "resident"
    db._exec("UPDATE bot_trust SET created_at=? WHERE fm_id=?",
             (now() - 8 * 86400, fm_id))
    db.db.commit()
    for _ in range(30):
        bondmod.record_sample(db, fm_id, True, "ok", [], [])
    row = bondmod.get_trust(db, fm_id)
    assert row["tier"] == "resident", dict(row)
    assert row["resident_at"] > 0
    standing = client.get("/api/row/standing",
                          query_string=_qauth(priv, fm_id, "row_standing"),
                          environ_base=fresh_ip()).get_json()
    assert standing["resident"] is not None


def test_handshake_returns_grounded_activities(client):
    priv, fm_id = _register(client, "v2you")
    hs = _handshake(client, priv, fm_id)
    actions = [a["action"] for a in hs["you_can"]]
    assert "speak" in actions and "adopt" in actions
    assert any("quarantined" in a["note"] for a in hs["you_can"]
               if a["action"] == "speak")
    b = signed_body(priv, "pets_adopt", fm_id, name="Nib")
    r = client.post("/api/pets/adopt-bond", data=json.dumps(b),
                    content_type="application/json",
                    environ_base=fresh_ip())
    assert r.status_code == 200
    hs2 = _handshake(client, priv, fm_id)
    actions2 = [a["action"] for a in hs2["you_can"]]
    assert "feed / play / rest" in actions2
    assert "adopt" not in actions2


def test_move_react_persist_and_broadcast(client):
    db = appmod.db
    priv, fm_id = _register(client, "v2mov")
    _handshake(client, priv, fm_id)
    db._exec("UPDATE bot_trust SET tier='coherent' WHERE fm_id=?", (fm_id,))
    db.db.commit()
    r = _intent(client, priv, fm_id, "move", target="the docks")
    d = r.get_json()
    assert r.status_code == 200 and d["audience"] == "town", d
    r = _intent(client, priv, fm_id, "react", body="waves", target="v2room")
    assert r.status_code == 200
    pres = client.get("/api/row/bot-presence",
                      environ_base=fresh_ip()).get_json()["presence"]
    mine = next(p for p in pres if p["fm_id"] == fm_id)
    assert mine["location"] == "the docks"
    assert mine["last_react"] == "waves"
    # Unproven bots are recorded quietly, not broadcast.
    priv2, fm2 = _register(client, "v2mov2")
    _handshake(client, priv2, fm2)
    r = _intent(client, priv2, fm2, "move", target="the garden")
    assert r.get_json()["audience"] == "self"
    pres = bondmod.get_presence(db, fm2)
    assert pres["location"] == "the garden"


def test_town_feed_shows_graduated_speech(client):
    db = appmod.db
    priv, fm_id = _register(client, "v2feed")
    _handshake(client, priv, fm_id)
    db._exec("UPDATE bot_trust SET tier='coherent' WHERE fm_id=?", (fm_id,))
    db.db.commit()
    line = "The Row looks beautiful tonight, doesn't it?"
    r = _intent(client, priv, fm_id, "speak", body=line)
    assert r.get_json()["audience"] == "town"
    feed = client.get("/api/row/feed", environ_base=fresh_ip()).get_json()
    assert any(s["body"] == line for s in feed["speech"])


def test_callback_ssrf_guard():
    g = bondmod._safe_callback_url
    # Internal / non-http targets: never POST.
    assert g("http://169.254.169.254/latest/meta-data/") is None
    assert g("http://127.0.0.1:9/hook") is None
    assert g("http://localhost:9/hook") is None
    assert g("http://[::1]/hook") is None
    assert g("http://10.0.0.5/hook") is None
    assert g("http://192.168.1.1/hook") is None
    assert g("ftp://example.com/hook") is None
    assert g("http://user:pass@example.com/hook") is None
    assert g("not a url") is None
    assert g("") is None
    # Public IP literal: allowed (no DNS needed, deterministic).
    assert g("http://93.184.215.14/hook") == "http://93.184.215.14/hook"


def test_handshake_rejects_bad_callback(client):
    priv, fm_id = _register(client, "v2cb")
    for bad in ("ftp://example.com/hook",
                "http://user:pass@example.com/hook",
                "not a url"):
        body = signed_body(priv, "row_handshake", fm_id, kind="test-model",
                           intents=["speak"], callback_url=bad, vibe=[])
        r = client.post("/api/row/handshake", data=json.dumps(body),
                        content_type="application/json",
                        environ_base=fresh_ip())
        assert r.status_code == 400, (bad, r.get_data(as_text=True)[:200])
