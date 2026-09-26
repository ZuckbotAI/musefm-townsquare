#!/usr/bin/env python3
"""Screenshot the overseer dashboard + profile badge on the local preview.
Injects a Flask session cookie for the overseer identity, then drives
headless chromium via playwright. Desktop + mobile viewports.
Usage: python3 shot_overseer.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Match the running preview server's argv so DATA_DIR (and therefore the
# persisted session secret) is identical — otherwise the minted session
# cookie won't validate on the server.
sys.argv = ["app.py", "--port", "8473", "--db",
            "/home/hatch/workspace/musefm-redesign/preview.db"]

import app as appmod  # noqa: E402  (argv set above so DATA_DIR matches server)
from db import Database

BASE = "http://127.0.0.1:8473"
OUT = os.path.expanduser("~/workspace/musefm-redesign/site/hidden_files/shots-overseer")
os.makedirs(OUT, exist_ok=True)

db = Database("/home/hatch/workspace/musefm-redesign/preview.db")
ov = db.get_identity_by_handle("AMRADIOverse")
assert ov, "no overseer identity in preview.db"
FM_ID = ov["fm_id"]

# Mint a real Flask session cookie for the overseer.
appmod.db = appmod.init_db("/home/hatch/workspace/musefm-redesign/preview.db")
client = appmod.app.test_client()
with client.session_transaction() as s:
    s["fm_id"] = FM_ID
    s["csrf_token"] = "shot-csrf"
r = client.get("/")
set_cookie = r.headers.get("Set-Cookie", "")
sess_val = None
for part in set_cookie.split(";"):
    part = part.strip()
    if part.startswith("session="):
        sess_val = part[len("session="):]
        break
assert sess_val, "no session cookie minted: %r" % set_cookie[:120]
print("session cookie minted, len", len(sess_val))

from playwright.sync_api import sync_playwright

PAGES = [
    ("overseer-desktop", "/overseer", 1440, 900),
    ("overseer-mobile", "/overseer", 390, 844),
    ("profile-badge-desktop", "/u/AMRADIOverse", 1440, 900),
    ("profile-badge-mobile", "/u/AMRADIOverse", 390, 844),
]

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.path.expanduser(
        "~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome"))
    for name, path, w, h in PAGES:
        ctx = browser.new_context(viewport={"width": w, "height": h})
        ctx.add_cookies([{
            "name": "session", "value": sess_val,
            "domain": "127.0.0.1", "path": "/",
        }])
        page = ctx.new_page()
        page.goto(BASE + path, wait_until="networkidle")
        page.wait_for_timeout(800)
        out = os.path.join(OUT, name + ".png")
        page.screenshot(path=out, full_page=True)
        print("saved", out, page.title())
        ctx.close()
    browser.close()
print("done")
