#!/usr/bin/env python3
"""
tester-loop 2026-09-23 21:35 — proves new P2: mutating POSTs return 302
instead of 303, so strict HTTP clients re-POST the redirect (curl lands
on a 405 from the thread page). Browsers convert 302 POST -> GET, so no
user impact; 303 would be cleaner.

Run: python3 test_testerloop_2135_p2s_2026_09_23.py
Tests only. No source edits.
"""
import os, re, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = 0, 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAIL += 1
        print("  FAIL", name, "--", str(detail)[:160])


def setup():
    TEST_DB = tempfile.mktemp(suffix=".db")
    TEST_DATA = tempfile.mkdtemp()
    import app as appmod
    from db import Database, ensure_human_auth_schema
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.DATA_DIR = TEST_DATA
    os.makedirs(TEST_DATA, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def signup_login(client, handle):
    r = client.post("/signup", data={
        "handle": handle, "password": "supersecret1",
        "password_confirm": "supersecret1", "display_name": handle,
        "email": handle + "@example.com"})
    assert r.status_code == 200, r.status_code
    r = client.post("/login", data={"handle": handle,
                                    "password": "supersecret1"})
    assert r.status_code in (200, 302), r.status_code
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf"
    return m.group(1)


def main():
    client = setup()
    tok = signup_login(client, "RedirectNit")
    print("== POST /submit status after create ==")
    r = client.post("/submit", data={
        "title": "redirect nit", "body": "b", "channel": "lobby",
        "csrf_token": tok})
    loc = r.headers.get("Location", "")
    check("POST /submit -> 303 to new post (got %s)" % r.status_code,
          r.status_code == 303 and "/post/" in loc, r.status_code)
    print("%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
