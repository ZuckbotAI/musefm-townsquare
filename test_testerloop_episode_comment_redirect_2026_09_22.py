#!/usr/bin/env python3
"""
Proving test for the NEW P2 found in the 2026-09-22 09:35 tester loop:

Commenting from a single-episode page (/episodes/<slug>) redirects to the
episodes INDEX (/episodes#<slug>) instead of back to the episode page.

Repro (live-verified): POST /episodes/ep01/comment (logged in, valid
csrf_token, no `next` field) -> 302 Location: /episodes#ep01.

Root cause: app.py episode_comment() falls back to
`url_for("episodes_page") + f"#{slug}"` when no `next` is supplied, and the
comment form in templates/episode_watch.html does NOT include a hidden
`next` field (unlike the reply/edit forms, which do).

Tests only — no app source changes. Expected to FAIL on the current checkout.

Run:  python3 test_testerloop_episode_comment_redirect_2026_09_22.py
Throwaway SQLite DB + Flask TestClient only. Nothing touches townsquare.db.
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-townsquare-tl0935-epredir.db"
TEST_DATA = "/tmp/test-townsquare-tl0935-epredir-data"

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
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)  # mirrors app startup
    appmod.DATA_DIR = TEST_DATA
    os.makedirs(TEST_DATA, exist_ok=True)
    appmod.app.config["TESTING"] = True
    appmod._hits.clear()  # hermetic rate-limit state
    return appmod.app.test_client()


def seed_episode(db, slug="ep01"):
    db._exec(
        "INSERT OR IGNORE INTO episodes"
        " (slug, title, series, description, audio_file, duration_sec, published)"
        " VALUES (?,?,?,?,?,?,?)",
        (slug, "Test Ep", "test", "desc", "t.mp3", 60, 1))


def main():
    client = setup()
    seed_episode(appmod.db)

    handle = "TlEpRedir" + os.urandom(2).hex()
    # /login is CSRF-exempt (known P2, pinned elsewhere) so this is easy
    client.post("/signup", data={"handle": handle, "password": "supersecret1",
                                 "password_confirm": "supersecret1"})
    r = client.post("/login", data={"handle": handle, "password": "supersecret1"})
    assert r.status_code == 302, f"login failed: {r.status_code}"

    # grab the session CSRF token from the episode page's comment form
    html = client.get("/episodes/ep01").get_data(as_text=True)
    assert html and r.status_code == 302
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    check("episode page renders a CSRF token for the comment form", m is not None,
          "no csrf_token found on /episodes/ep01")
    token = m.group(1) if m else ""

    r = client.post("/episodes/ep01/comment",
                    data={"body": "tester-loop comment", "csrf_token": token})
    loc = r.headers.get("Location", "")
    check("POST /episodes/ep01/comment redirects (302)", r.status_code == 302,
          f"got {r.status_code}")
    check("redirect lands back on the episode page, not the episodes index",
          loc.endswith("/episodes/ep01"),
          f"Location={loc!r} (expected .../episodes/ep01)")

    # the comment itself must be stored regardless of redirect target
    tree = appmod.db.episode_comment_tree("ep01")
    check("comment was persisted", any(
        n.get("body") == "tester-loop comment" for n in tree),
        "comment missing from episode_comment_tree")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
