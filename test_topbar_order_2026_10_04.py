#!/usr/bin/env python3
"""
Topbar order test (2026-10-04, overhaul item 3: top bar reorder, desktop).

Proves the left-to-right order inside <div class="topbar-right">:
  logged in:  messages (dm-mail) -> notifications (notif-bell) ->
              user login tag (handle-chip) -> dark-mode toggle (theme-btn) ->
              search (topbar-search) -> log out (logout-inline, far right)
  logged out: handle-chip -> theme-btn -> topbar-search -> Log in -> Sign up

Also proves:
  - the search form still points at /c/lobby with a q input (search stays clean)
  - the dark-mode toggle still renders with both icons and the toggleTheme()
    hook wired (JS function defined on the page)
  - the responsive CSS keeps the bar usable on phones: at <=860px the
    search and the handle chip hide, and log out pins to the far right

Run:  python3 test_topbar_order_2026_10_04.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-topbar-order.db"
TEST_DATA = "/tmp/test-townsquare-topbar-order-data"

PASS, FAIL = [], []
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = appmod.init_db(TEST_DB)  # full schema ensure, like startup
    appmod.DATA_DIR = TEST_DATA
    os.makedirs(TEST_DATA, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def topbar_slice(html):
    """Return just the topbar-right markup so order assertions cannot
    match identical strings rendered elsewhere on the page."""
    m = re.search(r'<div class="topbar-right">(.*?)</div>\s*</header>',
                  html, re.S)
    assert m, "topbar-right not found in rendered page"
    return m.group(1)


def assert_order(name, html, markers):
    idx = [html.find(m) for m in markers]
    missing = [m for m, i in zip(markers, idx) if i < 0]
    ordered = all(a < b for a, b in zip(idx, idx[1:]))
    check(name, not missing and ordered,
          f"missing={missing} idx={idx}" if missing or not ordered else "")


def main():
    client = setup()

    print("== logged-out topbar order ==")
    html = topbar_slice(client.get("/").get_data(as_text=True))
    assert_order("anon: chip, theme, search, log in, sign up",
                 html, ['id="handle-chip"', 'class="theme-btn"',
                        'class="topbar-search"', 'href="/login"',
                        'href="/signup"'])
    check("anon: no messages icon", 'class="dm-mail' not in html)
    check("anon: no notifications bell", 'id="notif-bell-btn"' not in html)
    check("anon: no log out form", 'class="logout-inline"' not in html)

    print("== logged-in topbar order ==")
    client.post("/signup", data={"handle": "topbarhuman",
                                 "password": "s3cretpw!!",
                                 "password_confirm": "s3cretpw!!",
                                 "email": "topbarhuman@example.com"})
    client.post("/login", data={"handle": "topbarhuman",
                                "password": "s3cretpw!!"})
    html = topbar_slice(client.get("/").get_data(as_text=True))
    assert_order("logged in: messages, notifications, tag, theme, search, log out",
                 html, ['class="dm-mail', 'id="notif-bell-btn"',
                        'id="handle-chip"', 'class="theme-btn"',
                        'class="topbar-search"', 'class="logout-inline"'])
    check("handle chip shows the login tag", "u/topbarhuman" in html)
    check("log out is a POST form to /logout",
          re.search(r'<form[^>]*method="post"[^>]*action="/logout"[^>]*class="logout-inline"',
                    html) is not None or
          re.search(r'<form[^>]*class="logout-inline"[^>]*action="/logout"', html) is not None)

    print("== search stays clean ==")
    check("search posts to /c/lobby", 'action="/c/lobby"' in html)
    check("search has the q input", 'name="q"' in html)
    check("search keeps its placeholder",
          "Search muses, topics, skills, or ideas..." in html)

    print("== dark-mode toggle still works ==")
    page = client.get("/").get_data(as_text=True)
    check("theme button present", 'class="theme-btn"' in html)
    check("theme button carries both moon and sun icons",
          'class="icon-moon"' in html and 'class="icon-sun"' in html)
    check("theme button calls toggleTheme()",
          'onclick="toggleTheme()"' in html)
    js = open(os.path.join(HERE, "static", "js", "app.js"),
              encoding="utf-8").read()
    check("page loads the app JS bundle",
          'static/js/app.js' in page or 'src="/static/js/app.js"' in page)
    check("toggleTheme() is defined in the app JS",
          re.search(r"function toggleTheme\s*\(", js) is not None)

    print("== mobile CSS keeps the bar uncluttered ==")
    css = open(os.path.join(HERE, "static", "css", "redesign.css"),
               encoding="utf-8").read()
    # collect every @media (max-width: 860px) block (there is more than one)
    bodies = []
    for m in re.finditer(r"@media\s*\(max-width:\s*860px\)\s*\{", css):
        depth, i = 0, m.end() - 1
        while i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        bodies.append(css[m.end():i])
    mob = " ".join(bodies)
    check("860px media block exists", bool(bodies))
    if bodies:
        check("search hides on phones",
              re.search(r"\.topbar\s+\.topbar-search\s*\{\s*display:\s*none", mob) is not None)
        check("handle chip hides on phones",
              re.search(r"#handle-chip\s*\{\s*display:\s*none", mob) is not None)
        check("log out pins to the far right on phones",
              re.search(r"\.logout-inline\s*\{\s*margin-left:\s*auto", mob) is not None)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
