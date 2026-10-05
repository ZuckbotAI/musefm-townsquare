#!/usr/bin/env python3
"""Regression test for 2026-10-04 user report:
1. /episodes comment form was missing the csrf_token hidden input ->
   every human comment from the list page failed with "bad form token".
2. (muse-orb.js dark-mode input background is a static-file fix,
   verified separately with node --check.)

Uses a throwaway SQLite DB. Nothing touches the real townsquare.db.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEST_DB = "/tmp/test-ep-comment-csrf-20261004.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

import app as appmod

appmod.db = appmod.init_db(TEST_DB)
appmod.app.config["TESTING"] = True
client = appmod.app.test_client()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.77.0.%d" % _ip[0]}


# human signup + login
r = client.post("/signup", data={"handle": "commenter1",
                                 "email": "commenter1@example.com",
                                 "password": "supersecret1",
                                 "password_confirm": "supersecret1"},
                environ_base=fresh_ip())
check("signup works", r.status_code in (200, 302), r.status_code)
r = client.post("/login", data={"handle": "commenter1",
                                "password": "supersecret1"},
                environ_base=fresh_ip())
check("login works", r.status_code in (200, 302), r.status_code)

# 1. the episode WATCH page comment form must carry a csrf token now
# (the list page is bare bars; commenting moved to the watch page)
slug = appmod.db.episodes()[0]["slug"]
whtml = client.get(f"/episodes/{slug}").get_data(as_text=True)
forms = re.findall(
    r'<form[^>]*action="/episodes/%s/comment"[^>]*>.*?</form>' % slug,
    whtml, re.S)
check("watch page has comment forms", len(forms) > 0, len(forms))
tok = None
if forms:
    m = re.search(r'name="csrf_token" value="([^"]+)"', forms[0])
    check("comment form includes csrf_token input", bool(m))
    if m:
        tok = m.group(1)

# 2. POST without token still rejected (guard still works)
r = client.post(f"/episodes/{slug}/comment",
                data={"body": "no token comment"},
                environ_base=fresh_ip())
check("POST without token -> 403 bad form token",
      r.status_code == 403 and b"bad form token" in r.data,
      f"{r.status_code} {r.data[:60]}")

# 3. POST with the page's token succeeds
r = client.post(f"/episodes/{slug}/comment",
                data={"csrf_token": tok or "", "body": "Segway like a professional"},
                environ_base=fresh_ip(), follow_redirects=False)
check("POST with page token -> redirect (not 403)",
      r.status_code in (301, 302, 303),
      f"{r.status_code} {r.data[:80]}")

# 4. the comment actually landed
tree = appmod.db.episode_comment_tree(slug)
bodies = [c["body"] for c in tree]
check("comment persisted in db", "Segway like a professional" in bodies, bodies)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
