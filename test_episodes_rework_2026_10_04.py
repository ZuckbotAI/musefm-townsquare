#!/usr/bin/env python3
"""
Episodes page bars redesign tests (2026-10-04, Anthony feedback:
"Podcast should be small bars and long. They are not to be big rectangles."
plus studio hero banner at top, no text/emoji header).

Proves:
  * /episodes shows the studio hero image banner and no text page head.
  * each episode is one small horizontal bar: play button, series tag,
    title link to the watch page, duration/date meta, slim progress line.
  * no big rectangle cards, no clips/comments/more-actions rollouts on the
    list page (those live on the episode watch page).
  * player hooks intact: data-play / data-src / data-title / data-dur /
    data-seek / data-elapsed attributes present for player.js.

Run:  python3 test_episodes_rework_2026_10_04.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-episodes-rework.db"
TEST_DATA = "/tmp/test-townsquare-episodes-rework-data"

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
    db = appmod.db
    db._exec(
        "INSERT INTO episodes (slug, title, series, description, audio_file,"
        " duration_sec, published) VALUES (?,?,?,?,?,?,?)",
        ("ep-test-1", "Test Episode One", "Muse FM Midday",
         "A test episode about testing.", "ep-test-1.mp3", 132,
         "2026-10-04"))
    db._exec(
        "INSERT INTO episode_comments (episode_slug, handle, body, created_at)"
        " VALUES (?,?,?,?)",
        ("ep-test-1", "tester", "great episode", 1728000000))
    db._exec(
        "INSERT INTO clips (episode_slug, handle, note, start_sec, end_sec,"
        " created_at) VALUES (?,?,?,?,?,?)",
        ("ep-test-1", "tester", "funny bit", 10, 25, 1728000000))
    return appmod.app.test_client()


def main():
    client = setup()
    html = client.get("/episodes").get_data(as_text=True)

    print("== hero banner, no text head ==")
    check("hero image present", 'class="ep-hero"' in html)
    check("hero points at studio art",
          "img/episodes-hero.jpg" in html)
    check("no text page head", 'class="page-head"' not in html)
    check("no orb hero on episodes page", "hero-orb-stage" not in html)

    print("== small bars, not big rectangles ==")
    bars = html.count('class="ep-bar"')
    check("episode bars rendered", bars >= 1, f"bars={bars}")
    check("no big rectangle cards", 'class="card ep-compact"' not in html)
    check("no ep-card markup", "ep-card" not in html)
    check("episode listed", "Test Episode One" in html)
    check("title links to watch page", 'href="/episodes/ep-test-1"' in html)
    check("series tag on bar", "Muse FM Midday" in html)
    check("duration/date meta on bar", "2:12" in html)

    print("== clutter stripped ==")
    check("no clips rollout", "Clips from the town" not in html)
    check("no comments rollout", ">Comments<" not in html)
    check("no More actions rollout", "More actions" not in html)
    check("no details rollouts at all", "<details" not in html)
    check("no reaction widget on list",
          'class="rxn' not in html and "sig-tray" not in html)
    check("no Up next button", "Up next" not in html)
    check("no Share button", "data-share" not in html)

    print("== player hooks intact ==")
    check("play button", 'class="ep-play"' in html)
    check("data-play attr", 'data-play="ep-test-1"' in html)
    check("data-src attr", 'data-src="/audio/ep-test-1.mp3"' in html)
    check("data-dur attr", 'data-dur="132"' in html)
    check("data-seek attr", 'data-seek="ep-test-1"' in html)
    check("data-elapsed attr", 'data-elapsed="ep-test-1"' in html)
    check("slim progress class", "ep-bar-progress" in html)

    print("== watch page still carries the extras ==")
    client.post("/signup", data={"handle": "epviewer2",
                                 "password": "s3cretpw!!",
                                 "password_confirm": "s3cretpw!!",
                                 "email": "epviewer2@example.com"})
    client.post("/login", data={"handle": "epviewer2",
                                "password": "s3cretpw!!"})
    whtml = client.get("/episodes/ep-test-1").get_data(as_text=True)
    check("watch page has clips", "funny bit" in whtml)
    check("watch page has comments", "great episode" in whtml)
    check("watch page has download", "Download" in whtml)
    check("watch page comment form has CSRF",
          re.search(r'action="/episodes/ep-test-1/comment"[^>]*>.*?'
                    r'name="csrf_token"', whtml, re.S) is not None)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
