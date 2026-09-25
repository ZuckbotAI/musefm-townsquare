#!/usr/bin/env python3
"""
Tests for POST /api/drift/adopt — the in-town Pet Shop adopt endpoint.

Covers: session auth (401 anonymous, server-resolved identity — client
fm_id/handle fields ignored), confirm:true required (400 typed),
CSRF (403 missing/bad, 200 good), typed error codes, rate limit
(drift_adopt 20/hr — 429 with Retry-After; failed validations never
consume budget), happy path returning the pet summary.

Run:  .venv/bin/python test_drift_adopt.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod

TEST_DB = "/tmp/test-townsquare-drift-adopt.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_pubkey():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.public_key().public_bytes_raw())


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ipn = [0]


def fresh_ip():
    _ipn[0] += 1
    return {"REMOTE_ADDR": f"10.77.{_ipn[0] // 250}.{_ipn[0] % 250 + 1}"}


def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


def signup_login(c, handle):
    """Real human /signup + /login flow; returns (client, csrf_token)."""
    ip = fresh_ip()
    r = c.post("/signup", data={"handle": handle,
                                "password": "supersecret1",
                                "password_confirm": "supersecret1",
                                "email": f"{handle.lower()}@example.com"},
               environ_base=ip)
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    r = c.post("/login", data={"handle": handle,
                               "password": "supersecret1"},
               environ_base=ip)
    assert r.status_code == 302, r.get_data(as_text=True)[:200]
    return c, csrf_of(c)


def session_client(appmod, db, handle, ip):
    """Bulk identity: registered directly, session injected server-side."""
    ident = db.register_identity(handle, fresh_pubkey())
    c = appmod.app.test_client()
    with c.session_transaction() as s:
        s["fm_id"] = ident["fm_id"]
        s["csrf_token"] = f"tok-{handle}"
    return c, ident["fm_id"]


def adopt_post(c, body, ip):
    return c.post("/api/drift/adopt", json=body, environ_base=ip)


def good_body(tok, species="bloop", name="Drifty"):
    return {"species": species, "name": name, "confirm": True,
            "csrf_token": tok}


def main():
    c = setup()
    db = appmod.db

    print("== auth: anonymous rejected ==")
    ip = fresh_ip()
    r = adopt_post(c, good_body("whatever"), ip)
    d = r.get_json()
    check("anon -> 401", r.status_code == 401, r.status_code)
    check("anon typed not_signed_in", d.get("code") == "not_signed_in", d)
    check("anon signin_url present", d.get("signin_url") == "/login", d)

    print("== main human account ==")
    human = appmod.app.test_client()
    human, tok = signup_login(human, "DriftHuman")
    hip = fresh_ip()
    me = db.get_identity_by_handle("DriftHuman")

    print("== identity resolved server-side, body fm_id/handle ignored ==")
    other = db.register_identity("DriftOther", fresh_pubkey())
    r = adopt_post(human, dict(good_body(tok, name="ServerSide"),
                               fm_id=other["fm_id"], handle="DriftOther",
                               user_id=other["fm_id"]), hip)
    d = r.get_json()
    check("spoofed fm_id in body -> 200 anyway", r.status_code == 200, d)
    check("pet belongs to session identity",
          db._one("SELECT name FROM tidepals WHERE fm_id=?",
                  (me["fm_id"],)) is not None, d)
    check("spoofed identity got no pet",
          db._one("SELECT fm_id FROM tidepals WHERE fm_id=?",
                  (other["fm_id"],)) is None)

    print("== confirm required ==")
    hum2 = appmod.app.test_client()
    hum2, tok2 = signup_login(hum2, "DriftConf")
    cip = fresh_ip()
    body = good_body(tok2, name="ConfirmMe")
    for label, val in [("missing", None), ("false", False),
                       ("string 'true'", "true"), ("int 1", 1)]:
        b = dict(body)
        if val is None:
            b.pop("confirm")
        else:
            b["confirm"] = val
        r = adopt_post(hum2, b, cip)
        d = r.get_json()
        check(f"confirm={label} -> 400 confirm_required",
              r.status_code == 400 and d.get("code") == "confirm_required",
              (r.status_code, d))
    check("no pet after unconfirmed attempts",
          db._one("SELECT fm_id FROM tidepals WHERE fm_id=?",
                  (db.get_identity_by_handle("DriftConf")["fm_id"],)) is None)

    print("== CSRF ==")
    hum3 = appmod.app.test_client()
    hum3, tok3 = signup_login(hum3, "DriftCsrf")
    sip = fresh_ip()
    r = adopt_post(hum3, {k: v for k, v in good_body(tok3).items()
                          if k != "csrf_token"}, sip)
    d = r.get_json()
    check("missing token -> 403 bad_csrf",
          r.status_code == 403 and d.get("code") == "bad_csrf",
          (r.status_code, d))
    b = good_body("wrong-token")
    r = adopt_post(hum3, b, sip)
    d = r.get_json()
    check("wrong token -> 403 bad_csrf",
          r.status_code == 403 and d.get("code") == "bad_csrf",
          (r.status_code, d))
    r = adopt_post(hum3, good_body(tok3, name="CsrfOk"), sip)
    d = r.get_json()
    check("valid token -> 200", r.status_code == 200, (r.status_code, d))

    print("== happy path summary ==")
    check("ok True", d.get("ok") is True, d)
    pet = d.get("pet") or {}
    check("pet summary has name", pet.get("name") == "CsrfOk", pet)
    check("pet summary has species", pet.get("species") == "bloop", pet)
    check("pet summary adopted", pet.get("adopted") is True, pet)

    print("== already adopted ==")
    r = adopt_post(hum3, good_body(tok3, name="Second"), sip)
    d = r.get_json()
    check("second adopt -> 409 already_adopted",
          r.status_code == 409 and d.get("code") == "already_adopted",
          (r.status_code, d))

    print("== validation typed errors ==")
    hum4 = appmod.app.test_client()
    hum4, tok4 = signup_login(hum4, "DriftValid")
    vip = fresh_ip()
    r = adopt_post(hum4, good_body(tok4, species="notaspecies"), vip)
    d = r.get_json()
    check("unknown species -> 400 invalid_adopt",
          r.status_code == 400 and d.get("code") == "invalid_adopt",
          (r.status_code, d))
    r = adopt_post(hum4, good_body(tok4, name="x"), vip)
    d = r.get_json()
    check("bad name -> 400 invalid_adopt",
          r.status_code == 400 and d.get("code") == "invalid_adopt",
          (r.status_code, d))
    r = adopt_post(hum4, good_body(tok4, species=123), vip)
    d = r.get_json()
    check("non-string species -> 400",
          r.status_code == 400, (r.status_code, d))
    r = adopt_post(hum4, good_body(tok4, species="zorb"), vip)
    d = r.get_json()
    check("locked species zorb -> 403 species_locked",
          r.status_code == 403 and d.get("code") == "species_locked",
          (r.status_code, d))
    r = adopt_post(hum4, good_body(tok4, name="ValidOne"), vip)
    check("valid adopt after 400s -> 200", r.status_code == 200,
          r.get_json())

    print("== malformed bodies ==")
    mip = fresh_ip()
    r = human.post("/api/drift/adopt", data="not json",
                   content_type="application/json", environ_base=mip)
    check("malformed JSON -> 400", r.status_code == 400, r.status_code)
    r = human.post("/api/drift/adopt", json=[1, 2], environ_base=mip)
    check("non-object JSON -> 400", r.status_code == 400, r.status_code)

    print("== rate limit: 20/hr bucket, 21st -> 429 ==")
    rip = {"REMOTE_ADDR": "10.88.0.9"}
    for i in range(20):
        cc, _ = session_client(appmod, db, f"RateA{i:02d}", rip)
        r = adopt_post(cc, {"species": "bloop", "name": f"Rate{i:02d}",
                            "confirm": True,
                            "csrf_token": f"tok-RateA{i:02d}"}, rip)
        if r.status_code != 200:
            check(f"rate bucket fill {i} -> 200", False,
                  (r.status_code, r.get_json()))
            break
    else:
        check("20 successful adoptions in window -> all 200", True)
    cc, _ = session_client(appmod, db, "RateA20", rip)
    r = adopt_post(cc, good_body("tok-RateA20", name="Rate20"), rip)
    d = r.get_json()
    check("21st adopt in window -> 429 rate_limited",
          r.status_code == 429 and d.get("code") == "rate_limited",
          (r.status_code, d))
    check("429 carries Retry-After", "Retry-After" in r.headers, r.headers)

    print("== rate limit: failed validations never consume budget ==")
    bip = {"REMOTE_ADDR": "10.88.0.10"}
    for i in range(10):
        cc, _ = session_client(appmod, db, f"RateB{i:02d}", bip)
        r = adopt_post(cc, {"species": "notaspecies", "name": f"B{i:02d}",
                            "confirm": True,
                            "csrf_token": f"tok-RateB{i:02d}"}, bip)
        if r.status_code != 400:
            check(f"invalid probe {i} -> 400", False, r.status_code)
            break
    else:
        check("10 invalid probes -> all 400", True)
    cc, _ = session_client(appmod, db, "RateBValid", bip)
    r = adopt_post(cc, good_body("tok-RateBValid", name="BudgetKept"), bip)
    check("valid adopt after 10 invalid probes -> 200 (budget untouched)",
          r.status_code == 200, (r.status_code, r.get_json()))

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
