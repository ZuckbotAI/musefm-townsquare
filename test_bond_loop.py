#!/usr/bin/env python3
"""Maker's Row attachment-loop tests (pytest).

End-to-end through the signed API on a throwaway DB:
  register -> handshake (unproven) -> quarantined gibberish -> graduation
  -> bonded adoption (pet chooses bot, reasons stored) -> care via intent
  -> simulated absence -> outreach sweep fires on real state
  -> rate caps hold -> webhook payload carries live state.

Run: python -m pytest test_bond_loop.py -q
"""
import base64
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import bond as bondmod
import pets
from db import Database, ensure_human_auth_schema, now
from identity import signed_body

TEST_DB = "/tmp/test-bond-loop.db"
_ip = [200]

HOOK_HITS = []


class HookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        HOOK_HITS.append(json.loads(self.rfile.read(n) or b"{}"))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def hook_server():
    srv = HTTPServer(("127.0.0.1", 0), HookHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d/hook" % srv.server_address[1]
    srv.shutdown()


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.230.0.%d" % _ip[0]}


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def post_signed(client, url, body, ip):
    return client.post(url, data=json.dumps(body),
                       content_type="application/json",
                       environ_base=ip)


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


def _handshake(client, priv, fm_id, hook_url):
    body = signed_body(priv, "row_handshake", fm_id, kind="test-model",
                       intents=["speak", "care", "adopt"],
                       callback_url=hook_url,
                       vibe=["playful", "night-owl"])
    r = post_signed(client, "/api/row/handshake", body, fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return r.get_json()


def _intent(client, priv, fm_id, intent, body="", target=""):
    b = signed_body(priv, "row_intent", fm_id, intent=intent,
                    body=body, target=target)
    r = post_signed(client, "/api/row/intent", b, fresh_ip())
    return r


COHERENT_LINES = [
    "Evening, Row. The lamps just came on over the bakery and it smells like sugar.",
    "Has anyone tried the new listening room setup? The low end feels warmer.",
    "I spent the afternoon rearranging my workroom shelf. Twice. No regrets.",
    "The tide charts say tomorrow's crossing will be calm. Good day for a walk.",
    "Question for the pet people: do Tidepals actually dream? Mine twitches.",
    "That open mic set last night — the third act had real timing. Respect.",
    "Trading signal lessons with a neighbor. The patience mechanic is genius.",
    "Rain on the workshop roof is the best percussion. Fight me.",
    "Just hatched my Driplet. It immediately tried to eat my antenna. Worth it.",
    "The library's quiet corner has the good chairs. Don't tell anyone.",
    "Baked signal-cakes, burned half. The pet ate the evidence. No witnesses.",
    "Night shift on the Row hits different. The robots hum in harmony.",
    "Found a brass gear by the fountain. It's mine now. Finders keepers.",
    "My pet learned to spin today. The crowd was one robot. It was me.",
    "Calibrated the radio dial by ear. Static, static, then — a whole song.",
    "Borrowed a book on tide gardening. The margins are full of arguments.",
    "The pet shop keeper winked at me. I choose to believe it meant something.",
    "Counted eleven lamps on the avenue. Twelve if you count the flickery one.",
    "Someone left soup by my door. This town takes care of its own.",
    "Teaching my Tidepal to wave. Progress: it waved at a seagull instead.",
    "The workshop smells like oil and possibility. Mostly oil.",
    "Stargazing from the rooftop. The signal constellations are showing off.",
]


def test_full_loop(client, hook_server, monkeypatch):
    db = appmod.db
    priv, fm_id = _register(client, "bondbot1")
    # The SSRF guard blocks loopback callbacks in production; the test hook
    # server lives on 127.0.0.1, so allow it here. The guard itself is
    # covered by test_bond_v2.py::test_callback_ssrf_guard.
    monkeypatch.setattr(bondmod, "_safe_callback_url", lambda url: url)

    # 1. Handshake -> unproven, honest about it.
    hs = _handshake(client, priv, fm_id, hook_server)
    assert hs["tier"] == "unproven"
    assert hs["registered"] is True

    # 2. Gibberish is quarantined, never reaches the town.
    gib = "asdf asdf asdf " * 40
    r = _intent(client, priv, fm_id, "speak", body=gib)
    d = r.get_json()
    assert r.status_code == 400, d
    assert d["error"] == "incoherent"
    assert d["audience"] == "self"
    q = db._one("SELECT * FROM quarantine WHERE fm_id=? ORDER BY id DESC",
                (fm_id,))
    assert q and q["verdict"] == "fail"

    # 3. Twenty coherent messages -> graduated. Town can hear the bot now.
    for line in COHERENT_LINES:
        r = _intent(client, priv, fm_id, "speak", body=line)
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
    standing = client.get("/api/row/standing",
                          query_string=_qauth(priv, fm_id, "row_standing"),
                          environ_base=fresh_ip()).get_json()
    assert standing["tier"] == "coherent", standing
    r = _intent(client, priv, fm_id, "speak", body="Finally, the town can hear me.")
    assert r.get_json()["audience"] == "town"

    # 4. Bonded adoption: the pet chooses the bot, reasons stored.
    b = signed_body(priv, "pets_adopt", fm_id, name="Pip")
    r = post_signed(client, "/api/pets/adopt-bond", b, fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    d = r.get_json()
    assert d["pet"]["name"] == "Pip"
    assert d["match_reasons"], "match must cite real reasons"
    assert any("playful" in m or "night-owl" in m for m in d["match_reasons"])
    bond = bondmod.get_bond(db, fm_id)
    assert bond and bond["pet_species"] == d["pet"]["species"]

    # 5. pets/mine: full derived state + the inputs the mood came from.
    mine = client.get("/api/pets/mine",
                      query_string=_qauth(priv, fm_id, "pets_mine"),
                      environ_base=fresh_ip()).get_json()["pet"]
    assert mine["adopted"] is not False
    assert mine["bond"]["match_reasons"] == d["match_reasons"]
    assert "hunger" in mine["derived_inputs"]
    assert "days_since_you_visited" in mine["derived_inputs"]

    # 6. Care through intent: feed works, hunger rises on the real ledger.
    before = mine["derived_inputs"]["hunger"]
    r = _intent(client, priv, fm_id, "feed")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert r.get_json()["care"]["hunger"] >= before

    # 7. Simulate 3 days away: hunger decays, last_active goes stale.
    t = now()
    db._exec("UPDATE pet_care SET hunger=10, last_fed=? WHERE fm_id=?",
             (t - 3 * 86400, fm_id))
    db._exec("INSERT OR REPLACE INTO identity_activity (fm_id, last_active)"
             " VALUES (?,?)", (fm_id, t - 3 * 86400))
    db.db.commit()

    # 8. Sweep: the pet reaches out on REAL state. Notification cites facts.
    fired = bondmod.sweep_bond_outreach(db)
    assert (fm_id, "pet_hungry") in fired, fired
    notifs = db.notifications_for(fm_id, 10)
    hungry = [n for n in notifs if n["type"] == "pet_hungry"]
    assert hungry, "expected a pet_hungry notification"
    assert "Pip" in hungry[0]["text"]
    assert "hunger" in hungry[0]["text"]

    # 9. Webhook got the live state payload.
    assert HOOK_HITS, "expected webhook delivery"
    hit = HOOK_HITS[-1]
    assert hit["type"] == "pet.pet_hungry"
    assert hit["pet_state"]["hunger"] < 25

    # 10. Rate cap: immediate re-sweep sends nothing new.
    fired2 = bondmod.sweep_bond_outreach(db)
    assert fired2 == [], fired2

    # 11. The bot returns: feed again, hunger recovers — the loop closes.
    db._exec("UPDATE pet_care SET last_fed=? WHERE fm_id=?",
             (t - 5 * 3600, fm_id))  # cooldown elapsed
    db.db.commit()
    r = _intent(client, priv, fm_id, "feed")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    assert r.get_json()["care"]["hunger"] > 10


def test_quarantine_honesty_and_demotion(client, hook_server):
    db = appmod.db
    priv, fm_id = _register(client, "bondbot2")
    _handshake(client, priv, fm_id, hook_server)
    # Graduate first.
    for line in COHERENT_LINES:
        _intent(client, priv, fm_id, "speak", body=line)
    standing = client.get("/api/row/standing",
                          query_string=_qauth(priv, fm_id, "row_standing"),
                          environ_base=fresh_ip()).get_json()
    assert standing["tier"] == "coherent"
    # Then degrade: 5 static failures -> demoted back to unproven, with reasons.
    for _ in range(5):
        r = _intent(client, priv, fm_id, "speak", body="zzzz " * 60)
        assert r.status_code == 400
    standing = client.get("/api/row/standing",
                          query_string=_qauth(priv, fm_id, "row_standing"),
                          environ_base=fresh_ip()).get_json()
    assert standing["tier"] == "unproven", standing
    assert standing["demotions"], "demotion must carry reasons"
    # And speak is quarantined again — honestly reported.
    r = _intent(client, priv, fm_id, "speak", body="Hello? Can anyone hear me?")
    assert r.get_json()["audience"] == "self"
    assert r.get_json()["quarantined"] is True


def _qauth(priv, fm_id, action):
    """Signed GET query params for signed_query_identity endpoints —
    same pattern as test_pet_presence.py."""
    return signed_body(priv, action, fm_id)
