#!/usr/bin/env python3
"""
Dead-space elimination tests (2026-10-04, overhaul item 4).

Proves that the reworked pages opt out of the narrow centered .wrap column
and use the new full-bleed layout primitives instead:

  * base.html: pages that set {% set page_layout = "full" %} render
    {{ self.content() }} directly, every other page keeps <div class="wrap">.
  * auth pages (login, signup, resend, success, verify, error): full-bleed
    band with a brand panel + form split (class auth-split), not the old
    440px centered column.
  * guide + api docs: full-bleed hero band + sticky section nav
    (hero-band, doc-nav), not the old 720px/960px columns.
  * doc pages (faq, facts, listed-on, memory, data-deletion, support,
    privacy, terms, service, passport): hero-band + full-inner column.

For every changed page the test asserts:
  1. the page renders (2xx, or the documented 400 for a bogus token),
  2. the old narrow-column wrapper <div class="wrap"> is ABSENT,
  3. the expected new layout marker is present,
  4. no old narrow max-width rule survives in the rendered HTML.
Plus CSS-level assertions on static/css/redesign.css.

Run:  .venv/bin/python test_deadspace_2026_10_04.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod

TEST_DB = "/tmp/test-townsquare-deadspace-2026-10-04.db"
TEST_DATA = "/tmp/test-townsquare-deadspace-2026-10-04-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.100.0.%d" % _ip[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register_muse(client, handle):
    priv = Ed25519PrivateKey.generate()
    pub = b64u(priv.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return b64u(priv.private_bytes_raw()), r.get_json()["fm_id"]


OLD_NARROW = [
    "max-width:720px",
    "max-width: 720px",
    "max-width: 960px",
    "max-width:440px",
    "max-width: 440px",
    ".auth-page { max-width: 440px",
]

# path -> (ok_statuses, expected layout marker)
PAGES = {
    "/login": ({200}, "auth-split"),
    "/signup": ({200}, "auth-split"),
    "/resend-verification": ({200}, "auth-split"),
    "/verify-email/bogus-token": ({400}, "auth-split"),
    "/guide": ({200}, "doc-nav"),
    "/api/docs": ({200}, "doc-nav"),
    "/faq": ({200}, "hero-band"),
    "/listed-on": ({200}, "hero-band"),
    "/memory": ({200}, "hero-band"),
    "/data-deletion": ({200}, "hero-band"),
    "/support": ({200}, "hero-band"),
    "/privacy": ({200}, "hero-band"),
    "/terms": ({200}, "hero-band"),
    "/trustline": ({200}, "hero-band"),
}


def main():
    client = setup()

    print("== pages render full-bleed (no .wrap, new marker present) ==")
    for path, (ok_statuses, marker) in PAGES.items():
        r = client.get(path, environ_base=fresh_ip())
        html = r.get_data(as_text=True)
        check(f"{path} renders", r.status_code in ok_statuses, r.status_code)
        check(f"{path} has no .wrap", '<div class="wrap">' not in html)
        check(f"{path} has marker {marker}", marker in html)
        for old in OLD_NARROW:
            check(f"{path} no old rule {old!r}", old not in html)

    print("== signup success page (POST /signup) ==")
    r = client.post("/signup",
                    data={"handle": "DeadSpaceHuman", "password": "supersecret1",
                          "password_confirm": "supersecret1",
                          "email": "deadspace-test@example.com"},
                    environ_base=fresh_ip())
    html = r.get_data(as_text=True)
    check("signup POST 200", r.status_code == 200, r.status_code)
    check("signup success has no .wrap", '<div class="wrap">' not in html)
    check("signup success has auth-split", "auth-split" in html)

    print("== auth_error page renders via request context ==")
    with appmod.app.test_request_context("/"):
        html = appmod.render_template("auth_error.html", error="boom")
    check("auth_error has no .wrap", '<div class="wrap">' not in html)
    check("auth_error has auth-split", "auth-split" in html)

    print("== passport page (registered muse) ==")
    _, fm_id = register_muse(client, "DeadSpaceMuse")
    r = client.get(f"/passport/{fm_id}", environ_base=fresh_ip())
    html = r.get_data(as_text=True)
    check("passport renders", r.status_code == 200, r.status_code)
    check("passport has no .wrap", '<div class="wrap">' not in html)
    check("passport has hero-band", "hero-band" in html)

    print("== a non-migrated page still keeps .wrap ==")
    r = client.get("/", environ_base=fresh_ip())
    html = r.get_data(as_text=True)
    check("homepage 200", r.status_code == 200, r.status_code)
    check("homepage keeps .wrap", '<div class="wrap">' in html)

    print("== base.html conditional ==")
    base = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "templates", "base.html")).read()
    check("base checks page_layout", 'page_layout == "full"' in base)
    check("base renders content directly", "{{ self.content() }}" in base)

    print("== redesign.css primitives ==")
    css = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "css", "redesign.css")).read()
    for cls in [".hero-band", ".doc-layout", ".doc-nav", ".auth-split",
                ".auth-panel", ".full-inner", ".full-col"]:
        check(f"css has {cls}", cls in css)
    check("css neutralizes old auth-page width",
          re.search(r"\.auth-page\.auth-wide\s*\{\s*max-width:\s*none", css) is not None)
    check("css auth-page is full-bleed",
          re.search(r"\.auth-page\s*\{\s*max-width:\s*none", css) is not None)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
