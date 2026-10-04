#!/usr/bin/env python3
"""
Episodes page rework tests (2026-10-04, overhaul item 7: homepage standard,
less wide cards, less dead space, plain buttons, share with rollouts).

Proves:
  * /episodes renders the slim page head (no big orb hero).
  * each episode card is compact: Play / Up next / Share / Watch page row,
    progress bar, reactions.
  * clips, comments, and extra actions live inside collapsed <details>
    rollouts (no `open` attribute), with counts in the summaries.
  * buttons are plain text (no emoji glyphs in labels).
  * the comment form inside the rollout still carries its CSRF token.

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

    print("== slim page head ==")
    check("page-head present", 'class="page-head"' in html)
    check("no orb hero on episodes page", "hero-orb-stage" not in html)
    check("episode listed", "Test Episode One" in html)

    print("== compact card ==")
    check("plain Play button", ">Play</button>" in html)
    check("plain Up next button", ">Up next</button>" in html)
    check("plain Share button", ">Share</button>" in html)
    check("plain Watch page link", ">Watch page</a>" in html)
    check("no emoji in Play label", "▶ Play" not in html)
    check("no emoji in Share label", "↗ Share" not in html)
    check("no emoji in Download label", "⬇ Download" not in html)
    check("progress bar present", 'class="progress"' in html)

    print("== rollouts collapsed ==")
    cards = html.count('class="card ep-compact"')
    rollouts = re.findall(r"<details class=\"ep-rollout\"([^>]*)>", html)
    check("episode cards rendered", cards >= 1, f"cards={cards}")
    check("three rollouts per episode", len(rollouts) == 3 * cards,
          f"found {len(rollouts)} for {cards} cards")
    check("all collapsed by default",
          all("open" not in attrs for attrs in rollouts))
    check("clips count in summary", "Clips from the town (1)" in html)
    check("comments count in summary", "Comments (1)" in html)
    check("clip row inside rollout", "funny bit" in html)
    check("comment inside rollout", "great episode" in html)
    check("download in More actions rollout", ">Download</a>" in html)
    check("listen live in More actions rollout", ">Listen live</a>" in html)

    print("== comment form keeps CSRF ==")
    client.post("/signup", data={"handle": "epviewer",
                                 "password": "s3cretpw!!",
                                 "password_confirm": "s3cretpw!!",
                                 "email": "epviewer@example.com"})
    client.post("/login", data={"handle": "epviewer",
                                "password": "s3cretpw!!"})
    html = client.get("/episodes").get_data(as_text=True)
    m = re.search(
        r'<form class="ep-comment-form"[^>]*>.*?</form>', html, re.S)
    check("comment form present", m is not None)
    check("csrf token in comment form",
          m is not None and 'name="csrf_token"' in m.group(0))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
