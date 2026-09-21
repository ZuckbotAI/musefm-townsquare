#!/usr/bin/env python3
"""Global login SSO provider tests (2026-09-21).

Covers the MuseFM-side provider: /auth/authorize (consent), /auth/token
(PKCE + one-time codes), /auth/pubkey, and the Ed25519 ID token contract.

Run:  .venv/bin/python test_sso_provider_2026_09_21.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import app as appmod

TEST_DB = "/tmp/test-townsquare-sso-20260921.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.202.0.%d" % _ip[0]}


def pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = b64u(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


CLIENT = "playbook"
REDIRECT = "https://x402-seller-a5et.onrender.com/auth/callback"
EVIL_REDIRECT = "https://evil.example.com/auth/callback"
RETIRED_CLIENT = "arena"  # retired 2026-09-21: must be rejected like any unknown client


def auth_params(**over):
    verifier, challenge = pkce()
    p = {"client_id": CLIENT, "redirect_uri": REDIRECT,
         "code_challenge": challenge, "code_challenge_method": "S256",
         "state": secrets.token_urlsafe(16)}
    p.update(over)
    return p, verifier


def signup_and_login(client):
    r = client.post("/signup", data={
        "handle": "ssohuman", "display_name": "SSO Human",
        "password": "supersecret1", "password_confirm": "supersecret1"},
        environ_base=fresh_ip(), follow_redirects=False)
    check("signup creates account", r.status_code in (200, 302),
          f"got {r.status_code}")
    r = client.post("/login", data={"handle": "ssohuman",
                                    "password": "supersecret1"},
                    environ_base=fresh_ip(), follow_redirects=False)
    check("login works", r.status_code == 302, f"got {r.status_code}")


def csrf_from(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def main():
    client = setup()

    # 1. schema
    cols = [r["name"] for r in appmod.db.db.execute(
        "PRAGMA table_info(sso_codes)")]
    check("sso_codes table exists", "code_hash" in cols and "used" in cols,
          str(cols))
    tables = [r[0] for r in appmod.db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check("sso_audit table exists", "sso_audit" in tables)

    # 2. pubkey
    r = client.get("/auth/pubkey", environ_base=fresh_ip())
    pk = r.get_json()
    check("GET /auth/pubkey 200", r.status_code == 200 and pk["ok"])
    check("pubkey is ed25519", pk.get("scheme") == "ed25519")
    pub = Ed25519PublicKey.from_public_bytes(b64u_decode(pk["public_key"]))
    check("pubkey decodes to 32 bytes",
          len(b64u_decode(pk["public_key"])) == 32)

    # 3. bad client / redirect / challenge -> 400, never a redirect
    p, _ = auth_params(client_id="nope")
    r = client.get("/auth/authorize?" + urllib.parse.urlencode(p),
                   environ_base=fresh_ip())
    check("unknown client_id -> 400", r.status_code == 400)
    p, _ = auth_params(client_id=RETIRED_CLIENT, redirect_uri=REDIRECT)
    r = client.get("/auth/authorize?" + urllib.parse.urlencode(p),
                   environ_base=fresh_ip())
    check("retired client_id (arena) -> 400", r.status_code == 400)
    p, _ = auth_params(redirect_uri=EVIL_REDIRECT)
    r = client.get("/auth/authorize?" + urllib.parse.urlencode(p),
                   environ_base=fresh_ip())
    check("unlisted redirect_uri -> 400 not redirect",
          r.status_code == 400 and "location" not in r.headers,
          f"status={r.status_code}")
    p, _ = auth_params(code_challenge_method="plain")
    r = client.get("/auth/authorize?" + urllib.parse.urlencode(p),
                   environ_base=fresh_ip())
    check("non-S256 challenge method -> 400", r.status_code == 400)
    p, _ = auth_params(state="")
    r = client.get("/auth/authorize?" + urllib.parse.urlencode(p),
                   environ_base=fresh_ip())
    check("empty state -> 400", r.status_code == 400)

    # 4. logged out -> sent to login, flow resumes after
    p, verifier = auth_params()
    r = client.get("/auth/authorize?" + urllib.parse.urlencode(p),
                   environ_base=fresh_ip(), follow_redirects=False)
    check("logged-out authorize -> 302 to /login",
          r.status_code == 302 and "/login?next=" in r.headers["location"],
          r.headers.get("location", ""))

    signup_and_login(client)

    # 5. consent screen
    r = client.get("/auth/authorize?" + urllib.parse.urlencode(p),
                   environ_base=fresh_ip())
    html = r.get_data(as_text=True)
    check("consent page 200", r.status_code == 200, f"got {r.status_code}")
    check("consent names the site + handle",
          "The Playbook" in html and "@ssohuman" in html)
    csrf = csrf_from(html)
    check("consent carries CSRF token", bool(csrf))

    # 6. bad CSRF on approve -> 403
    bad = dict(p, csrf_token="bogus", action="approve")
    r = client.post("/auth/authorize", data=bad, environ_base=fresh_ip())
    check("bad CSRF -> 403", r.status_code == 403, f"got {r.status_code}")

    # 7. deny -> error=access_denied at the client redirect
    deny = dict(p, csrf_token=csrf, action="deny")
    r = client.post("/auth/authorize", data=deny, environ_base=fresh_ip(),
                    follow_redirects=False)
    loc = r.headers.get("location", "")
    check("deny -> 302 with error=access_denied",
          r.status_code == 302 and "error=access_denied" in loc
          and "state=" + p["state"] in loc, loc)

    # 8. approve -> code + echoed state
    appr = dict(p, csrf_token=csrf, action="approve")
    r = client.post("/auth/authorize", data=appr, environ_base=fresh_ip(),
                    follow_redirects=False)
    loc = r.headers.get("location", "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    check("approve -> 302 with code + state",
          r.status_code == 302 and "code" in q
          and q["state"] == [p["state"]], loc)
    check("redirect stays on the registered URI",
          loc.startswith(REDIRECT), loc)
    code = q["code"][0]

    # 9. token happy path
    r = client.post("/auth/token", json={
        "client_id": CLIENT, "code": code, "code_verifier": verifier,
        "redirect_uri": REDIRECT}, environ_base=fresh_ip())
    body = r.get_json()
    check("token exchange 200", r.status_code == 200 and body["ok"],
          f"got {r.status_code} {body}")
    check("token returns fm_id + handle",
          body.get("handle") == "ssohuman" and body.get("fm_id", "").startswith("fm_"))
    id_token = body["id_token"]
    h_b, p_b, s_b = id_token.split(".")
    pub.verify(b64u_decode(s_b), (h_b + "." + p_b).encode())
    claims = json.loads(b64u_decode(p_b))
    check("id_token signature verifies", True)
    check("id_token claims",
          claims["iss"] == "https://musefm.lol"
          and claims["aud"] == CLIENT
          and claims["sub"] == body["fm_id"]
          and claims["handle"] == "ssohuman"
          and claims["exp"] - claims["iat"] == 600, str(claims))

    # 10. replay the same code -> rejected
    r = client.post("/auth/token", json={
        "client_id": CLIENT, "code": code, "code_verifier": verifier,
        "redirect_uri": REDIRECT}, environ_base=fresh_ip())
    check("replayed code -> 400", r.status_code == 400,
          f"got {r.status_code}")

    # 11. wrong verifier / client / redirect
    def fresh_code():
        pp, vv = auth_params()
        rr = client.get("/auth/authorize?" + urllib.parse.urlencode(pp),
                        environ_base=fresh_ip())
        tok = csrf_from(rr.get_data(as_text=True))
        rr = client.post("/auth/authorize",
                         data=dict(pp, csrf_token=tok, action="approve"),
                         environ_base=fresh_ip(), follow_redirects=False)
        qq = urllib.parse.parse_qs(
            urllib.parse.urlparse(rr.headers["location"]).query)
        return pp, vv, qq["code"][0]

    pp, vv, cc = fresh_code()
    r = client.post("/auth/token", json={
        "client_id": CLIENT, "code": cc, "code_verifier": "wrong-verifier",
        "redirect_uri": REDIRECT}, environ_base=fresh_ip())
    check("wrong verifier -> 400", r.status_code == 400)
    r = client.post("/auth/token", json={
        "client_id": "trustline", "code": cc, "code_verifier": vv,
        "redirect_uri": "https://trustlineapp.com/auth/callback"},
        environ_base=fresh_ip())
    check("cross-client code use -> 400", r.status_code == 400)
    r = client.post("/auth/token", json={
        "client_id": CLIENT, "code": cc, "code_verifier": vv,
        "redirect_uri": EVIL_REDIRECT}, environ_base=fresh_ip())
    check("mismatched redirect_uri -> 400", r.status_code == 400)

    # 12. expired code -> 400
    pp, vv, cc = fresh_code()
    ch = hashlib.sha256(cc.encode()).hexdigest()
    appmod.db.db.execute(
        "UPDATE sso_codes SET expires_at = ? WHERE code_hash = ?",
        (int(time.time()) - 1, ch))
    appmod.db.db.commit()
    r = client.post("/auth/token", json={
        "client_id": CLIENT, "code": cc, "code_verifier": vv,
        "redirect_uri": REDIRECT}, environ_base=fresh_ip())
    check("expired code -> 400", r.status_code == 400)

    # 13. tampered id_token fails client-side verification
    parts = id_token.split(".")
    tampered = parts[0] + "." + parts[1] + "." + b64u(b"x" * 64)
    try:
        pub.verify(b64u_decode(tampered.split(".")[2]),
                   (parts[0] + "." + parts[1]).encode())
        check("tampered id_token rejected", False, "verified a forgery!")
    except Exception:
        check("tampered id_token rejected", True)

    # 14. plaintext codes never persist
    rows = appmod.db.db.execute(
        "SELECT code_hash FROM sso_codes").fetchall()
    leaked = any(len(r[0]) != 64 for r in rows)
    check("only code hashes stored (64-hex)", not leaked and len(rows) > 0,
          f"{len(rows)} rows")

    # 15. audit trail written, no secrets
    audit = appmod.db.db.execute("SELECT event FROM sso_audit").fetchall()
    events = {r[0] for r in audit}
    check("audit has issue/redeem/deny events",
          {"code_issued", "code_redeemed", "consent_denied"} <= events,
          str(events))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
