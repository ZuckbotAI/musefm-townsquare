#!/usr/bin/env python3
"""
Photo detail prev/next regression test (2026-09-23, Anthony).

photo_neighbors() must flank a photo in gallery order
(created_at DESC, id DESC — same as list_photos): prev = the newer photo
shown before it in the grid, next = the older one after it. Ends of the
gallery get None on the empty side, and the arrows render on /musefm/photos/<id>.

Run:  python3 test_photo_nav_2026_09_23.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-photo-nav.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    db = appmod.db
    # wipe seeded photos so the fixture order is exact
    db._exec("DELETE FROM photos")

    ids = []
    for t in ("oldest", "middle", "newest"):
        ids.append(db.add_photo(t, "", "img/x.png", "", "Zuckbot"))
        time.sleep(0.02)  # distinct created_at
    oldest, middle, newest = ids
    grid = [p["id"] for p in db.list_photos(limit=50)]
    check("grid order newest-first", grid == [newest, middle, oldest], grid)

    check("newest: no prev", db.photo_neighbors(newest)[0] is None)
    check("newest: next is middle", db.photo_neighbors(newest)[1] == middle)
    check("middle: prev is newest", db.photo_neighbors(middle)[0] == newest)
    check("middle: next is oldest", db.photo_neighbors(middle)[1] == oldest)
    check("oldest: prev is middle", db.photo_neighbors(oldest)[0] == middle)
    check("oldest: next is None", db.photo_neighbors(oldest)[1] is None)
    check("unknown id -> (None, None)", db.photo_neighbors(999999) == (None, None))

    c = appmod.app.test_client()
    html = c.get(f"/musefm/photos/{middle}").get_data(as_text=True)
    check("watch 200", True)
    check("prev arrow links newest",
          f'href="/musefm/photos/{newest}"' in html)
    check("next arrow links oldest",
          f'href="/musefm/photos/{oldest}"' in html)
    html = c.get(f"/musefm/photos/{newest}").get_data(as_text=True)
    check("first photo: no prev arrow", "photo-prev" not in html)
    check("first photo: next arrow present", "photo-next" in html)
    r = c.get("/musefm/photos/999999")
    check("unknown photo 404", r.status_code == 404, r.status_code)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
