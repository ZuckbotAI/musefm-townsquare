#!/usr/bin/env python3
"""
Proving test for the NEW P2 found in the 2026-09-24 18:35 tester loop
(agent tester, verified live against the scratch instance):

a musefm-v1 signed body with the `signature` field present but EMPTY
reports "signature does not verify" instead of "missing signature".

This is the third distinct input in the missing-vs-malformed signature
message family (filed 15:35: absent -> "bad signature encoding";
this run: empty string -> "signature does not verify"). There is no
signature present to verify, so "does not verify" is inaccurate —
expected "missing signature", matching the timestamp/nonce guard pattern
(2026-09-21 missing timestamp -> "missing timestamp"; 2026-09-24 06:35
missing nonce -> "missing nonce").

Root cause (agent tester, verified in code): verify_signed_body
(identity.py:199) passes data.get("signature") into b64u_decode; ""
decodes to b"" without raising (length cap is on the string, not the
bytes), so it falls through to Ed25519 verify and is rejected as a bad
signature. The fix is the same guard the family asks for:
`if not signature: raise IdentityError("missing signature")`.

Tests only — no app source changes. The first test is expected to
FAIL on the current checkout; the two controls must PASS both before
and after the fix.

Run:  python3 -m pytest test_testerloop_empty_sig_2026_09_24.py -q
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import identity as idmod
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

TEST_DB = "/tmp/test-townsquare-tl241835-emptysig.db"

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
               data=json.dumps({"handle": "tl_emptysig_%d" % int(time.time() % 100000),
                                "public_key": _PUB_B64}),
               content_type="application/json")
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return c, r.get_json()["fm_id"]


def _signed(priv_b64, fm_id, **kw):
    return idmod.signed_body(priv_b64, "post", fm_id,
                             community="lobby", title="sig probe",
                             body="probe", **kw)


def test_empty_signature_reports_missing_not_does_not_verify():
    c, fm_id = _client()
    body = _signed(_PRIV_B64, fm_id)
    body["signature"] = ""  # present but empty — nothing to verify
    r = c.post("/api/forum/post", data=json.dumps(body),
               content_type="application/json")
    d = r.get_json() or {}
    assert r.status_code == 401, (r.status_code, d)
    assert "missing signature" in (d.get("error") or ""), d


def test_absent_signature_still_reports_bad_encoding():
    # control: pins the 15:35 P2's current (unfixed) behavior so the fix
    # for the empty case doesn't accidentally change the absent case
    c, fm_id = _client()
    body = _signed(_PRIV_B64, fm_id)
    del body["signature"]
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
