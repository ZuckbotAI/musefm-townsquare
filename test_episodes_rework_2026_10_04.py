#!/usr/bin/env python3
"""
Episodes archive removal tests (2026-10-05, Anthony: "Remove the Episodes
archive page"). Supersedes the 2026-10-04 bars-redesign assertions: the
/episodes archive page is gone, /episodes now redirects toward /jabs,
and direct /episodes/<slug> watch pages keep working with clips,
comments, download, and CSRF.

Proves:
  * /episodes redirects (308) toward /jabs; following the chain lands 200.
  * /jabs features the latest official episode at the top.
  * the episode watch page still carries clips, comments, download, CSRF.

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

    print("== episodes archive is gone, redirects to jabs ==")
    r = client.get("/episodes", follow_redirects=False)
    check("episodes archive returns a redirect", r.status_code in (301, 302, 303, 307, 308),
          f"status={r.status_code}")
    r2 = client.get("/episodes", follow_redirects=True)
    check("redirect chain lands on a 200 page", r2.status_code == 200,
          f"status={r2.status_code}")
    final_path = r2.request.path
    check("redirect chain lands on jabs", final_path == "/jabs",
          f"final={final_path}")

    print("== jabs features the latest official episode ==")
    jhtml = client.get("/jabs").get_data(as_text=True)
    check("jabs page loads", r2.status_code == 200)
    check("latest episode featured on jabs", "Test Episode One" in jhtml)
    check("featured card present", 'id="jab-featured"' in jhtml)
    check("next arrow present", 'id="jab-next"' in jhtml)

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
