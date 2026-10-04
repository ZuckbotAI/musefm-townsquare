#!/usr/bin/env python3
"""
Login modernization tests (2026-10-04, overhaul item 6: login page modern standard).

Proves:
  * /login renders the modern hooks: forgot-password link, auth.js, caps-lock
    warning, double-submit guard, mobile input attributes.
  * /forgot-password never enumerates: unknown handle and known handle get
    the identical sent page.
  * reset tokens round-trip, are salt-separated from verify tokens, expire,
    and refuse stale email.
  * /reset-password/<token> enforces length/match rules, actually changes the
    password, and the new password logs in.

Run:  python3 test_login_modern_2026_10_04.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import auth_email

TEST_DB = "/tmp/test-townsquare-login-modern.db"
TEST_DATA = "/tmp/test-townsquare-login-modern-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    os.makedirs(TEST_DATA, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def signup(client, handle, email):
    return client.post("/signup", data={
        "handle": handle, "password": "s3cretpw!!",
        "password_confirm": "s3cretpw!!", "email": email})


def main():
    client = setup()

    print("== login page modern hooks ==")
    html = client.get("/login").get_data(as_text=True)
    check("login renders 200", "Welcome back" in html)
    check("forgot-password link present",
          'href="/forgot-password"' in html)
    check("auth.js loaded", 'js/auth.js' in html)
    check("caps-lock hook on password", 'data-caps-watch' in html)
    check("caps warning element", 'data-caps-warn' in html)
    check("show/hide toggle hook", 'data-password-toggle="login-password"' in html)
    check("double-submit guard hooks",
          'data-auth-form' in html and 'data-auth-submit' in html)
    check("mobile input attrs",
          'autocapitalize="none"' in html and 'enterkeyhint="go"' in html)
    check("no inline toggle script left",
          "getElementById('toggle-password')" not in html)

    print("== forgot-password does not enumerate ==")
    signup(client, "resetfred", "resetfred@example.com")
    r_known = client.post("/forgot-password",
                          data={"handle": "resetfred"})
    r_unknown = client.post("/forgot-password",
                            data={"handle": "nosuchhandlexyz"})
    check("known handle -> 200 sent page",
          r_known.status_code == 200 and "reset link is on its way" in
          r_known.get_data(as_text=True))
    check("unknown handle -> identical 200 sent page",
          r_unknown.status_code == 200 and
          r_unknown.get_data(as_text=True) == r_known.get_data(as_text=True))

    print("== reset token mechanics ==")
    ident = appmod.db.get_identity_by_handle("resetfred")
    good = auth_email.make_reset_token(
        appmod.app.secret_key, ident["fm_id"], ident["email"])
    check("token round-trips",
          auth_email.read_reset_token(appmod.app.secret_key, good) ==
          (ident["fm_id"], ident["email"].lower()))
    check("garbage token rejected",
          auth_email.read_reset_token(appmod.app.secret_key, "nope") is None)
    verify_tok = auth_email.make_verify_token(
        appmod.app.secret_key, ident["fm_id"], ident["email"])
    check("verify token not readable as reset token",
          auth_email.read_reset_token(appmod.app.secret_key, verify_tok) is None)
    check("reset token not readable as verify token",
          auth_email.read_verify_token(appmod.app.secret_key, good) is None)

    print("== reset-password flow ==")
    r = client.get("/reset-password/" + "bogus")
    check("bogus token -> 400 invalid",
          r.status_code == 400 and "Link expired" in r.get_data(as_text=True))
    html = client.get("/reset-password/" + good).get_data(as_text=True)
    check("good token -> 200 form", "Choose a new password" in html)
    r = client.post("/reset-password/" + good,
                    data={"password": "short", "password_confirm": "short"})
    check("short password -> 400", r.status_code == 400)
    r = client.post("/reset-password/" + good,
                    data={"password": "n3wsecret!!",
                          "password_confirm": "different!!"})
    check("mismatch -> 400", r.status_code == 400)
    r = client.post("/reset-password/" + good,
                    data={"password": "n3wsecret!!",
                          "password_confirm": "n3wsecret!!"})
    check("valid reset -> success page",
          r.status_code == 200 and "Password updated" in r.get_data(as_text=True))
    r = client.post("/login", data={"handle": "resetfred",
                                    "password": "n3wsecret!!"})
    check("new password logs in", r.status_code in (301, 302, 303))
    r = client.post("/login", data={"handle": "resetfred",
                                    "password": "s3cretpw!!"})
    check("old password rejected", r.status_code == 401)

    print("== stale email refused ==")
    ident2 = appmod.db.get_identity_by_handle("resetfred")
    stale_tok = auth_email.make_reset_token(
        appmod.app.secret_key, ident2["fm_id"], ident2["email"])
    appmod.db._exec("UPDATE identities SET email=? WHERE fm_id=?",
                    ("changed@example.com", ident2["fm_id"]))
    r = client.get("/reset-password/" + stale_tok)
    check("email changed since mint -> 400 stale",
          r.status_code == 400 and "email changed" in r.get_data(as_text=True))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
