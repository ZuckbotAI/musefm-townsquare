#!/usr/bin/env python3
"""
Proving test for the NEW P2 found in the 2026-09-24 15:35 tester loop
(agent tester, verified live against the scratch instance + in code):
a musefm-v1 signed body with the `signature` field MISSING reports
"bad signature encoding" instead of "missing signature".

Same class as the two previously-fixed P2s:
  - missing timestamp -> "missing timestamp" (2026-09-21 06:35 loop)
  - missing nonce    -> "missing nonce"    (2026-09-24 06:35 loop)
The signature path never got the same absent-vs-malformed guard:
identity.py verify_signed_body passes data.get("signature") straight
into b64u_decode, so an absent (None) field raises the same
"bad signature encoding" as a garbage one, and clients cannot tell
the two apart.

Tests only — no app source changes. The first test is expected to
FAIL on the current checkout; the two controls document the intended
distinction (missing vs malformed) and must PASS both before and
after the fix.

Run:  python3 -m pytest test_testerloop_sig_missing_2026_09_24.py -q
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import identity as idmod
from db import Database, ensure_human_auth_schema
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

TEST_DB = "/tmp/test-townsquare-tl241535-sigmissing.db"

_priv = Ed25519PrivateKey.generate()
_PRIV_B64 = idmod.b64u_encode(
    _priv.private_bytes(serialization.Encoding.Raw,
                        serialization.PrivateFormat.Raw,
                        serialization.NoEncryption()))
_PUB_B64 = idmod.b64u_encode(
    _priv.public_key().public_bytes(serialization.Encoding.Raw,
                                    serialization.PublicFormat.Raw))


def _client():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)  # full schema + seeds, mirrors app startup
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    r = c.post("/api/identity/register",
               data=json.dumps({"handle": "tl_sigmissing_%d" % int(time.time() % 100000),
                                "public_key": _PUB_B64}),
               content_type="application/json")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return c, r.get_json()["fm_id"]


def _signed(priv_b64, fm_id, **kw):
    return idmod.signed_body(priv_b64, "post", fm_id,
                             community="lobby", title="sig probe",
                             body="probe", **kw)


def test_missing_signature_reports_missing_not_bad_encoding():
    c, fm_id = _client()
    body = _signed(_PRIV_B64, fm_id)
    del body["signature"]  # field absent entirely — correctly signed otherwise
    r = c.post("/api/forum/post", data=json.dumps(body),
               content_type="application/json")
    d = r.get_json() or {}
    assert r.status_code == 401, (r.status_code, d)
    assert "missing signature" in (d.get("error") or ""), d


def test_garbage_signature_still_reports_bad_encoding():
    c, fm_id = _client()
    body = _signed(_PRIV_B64, fm_id)
    body["signature"] = "!!!notb64!!!"  # present but malformed
    r = c.post("/api/forum/post", data=json.dumps(body),
               content_type="application/json")
    d = r.get_json() or {}
    assert r.status_code == 401, (r.status_code, d)
    assert "bad signature encoding" in (d.get("error") or ""), d


def test_valid_signed_body_still_accepted():
    c, fm_id = _client()
    body = _signed(_PRIV_B64, fm_id)
    r = c.post("/api/forum/post", data=json.dumps(body),
               content_type="application/json")
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:200])


if __name__ == "__main__":
    sys.exit(0)
