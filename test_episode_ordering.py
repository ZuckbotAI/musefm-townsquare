#!/usr/bin/env python3
"""
Regression test: episode listings are latest-first everywhere.

db.episodes() feeds /episodes, /musefm hub, the unified /shorts feed
(?series=musefm), and /api/episodes.
All of them must show the newest episode first (2026-09-23, Anthony:
"fix podcast list be latest first not just forum posts but also on forum").

Run: .venv/bin/python test_episode_ordering.py
Throwaway SQLite db. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import Database, ensure_musefm_media_schema
import videos

TEST_DB = "/tmp/test-townsquare-episode-ordering.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " -- " + str(detail)))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    db = Database(TEST_DB)
    videos.ensure_video_schema(db)
    ensure_musefm_media_schema(db)
    cols = ["slug", "title", "series", "description", "audio_file",
            "duration_sec", "published"]
    ins = "INSERT INTO episodes (%s) VALUES (%s)" % (
        ",".join(cols), ",".join(["?"] * len(cols)))
    # insert in scrambled order; published are ISO dates
    for slug, pub in [("old-one", "2026-09-17"), ("newest", "2026-09-23"),
                      ("middle", "2026-09-20")]:
        db._q(ins, (slug, slug, "s", "d", "a.mp3", 60, pub))
    got = [e["slug"] for e in db.episodes()]
    pubs = [e["published"] for e in db.episodes()]
    check("episodes() latest-first (non-increasing published)",
          all(pubs[i] >= pubs[i + 1] for i in range(len(pubs) - 1)), pubs)
    mine = [s for s in got if s in ("old-one", "newest", "middle")]
    check("inserted rows latest-first", mine == ["newest", "middle", "old-one"],
          mine)
    # same published date: deterministic tiebreak, newest slug first
    db._q(ins, ("tie-b", "b", "s", "d", "a.mp3", 60, "2026-09-23"))
    same_day = [e["slug"] for e in db.episodes()
                if e["published"] == "2026-09-23" and e["slug"] in ("tie-b", "newest")]
    check("same-day tiebreak deterministic", same_day == ["tie-b", "newest"],
          same_day)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
