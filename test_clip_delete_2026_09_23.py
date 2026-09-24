#!/usr/bin/env python3
"""
Clip delete + header-handle regression tests (2026-09-23, Anthony).

1. /episodes/<slug>/clips/<id>/delete:
   - owner (logged-in human whose session handle matches the clip handle,
     case-insensitive) can delete; clip row is gone; redirects to /episodes#slug
   - a different logged-in human gets 403 and the clip survives
   - anonymous POST redirects to /login
   - wrong/missing CSRF token -> 403
   - the delete affordance renders for the owner but not for strangers
2. Header "u/anon" bug: a logged-in human with NO ts_handle cookie must see
   u/<their handle> in the header chip, never u/anon (the route-level
   handle=_musefm_handle() cookie fallback used to clobber the session
   handle on /episodes and friends).

Run:  python3 test_clip_delete_2026_09_23.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-clip-delete.db"
TEST_DATA = "/tmp/test-townsquare-clip-delete-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.99.7.%d" % _ip[0]}


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


def signup_login(handle, password="supersecret1"):
    c = appmod.app.test_client()
    r = c.post("/signup", data={"handle": handle, "password": password,
                                "password_confirm": password,
                                "email": f"{handle.lower()}@example.com"},
               environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = c.post("/login", data={"handle": handle, "password": password},
               environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    html = c.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in human"
    return c, m.group(1)


def make_clip(client, slug, handle, start=0, end=10, note="oops"):
    r = client.post(f"/api/episodes/{slug}/clips",
                    json={"handle": handle, "start_sec": start,
                          "end_sec": end, "note": note},
                    environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()["id"]


def main():
    setup()
    me, tok = signup_login("ClipOwner")
    other, tok2 = signup_login("ClipStranger")
    anon = appmod.app.test_client()
    slug = "ep01"
    assert appmod.db.episode(slug), "seed episode missing"

    print("== clip delete: owner affordance ==")
    cid = make_clip(me, slug, "ClipOwner")
    html = me.get("/episodes").get_data(as_text=True)
    check("owner sees delete form",
          f"/episodes/{slug}/clips/{cid}/delete" in html)
    html2 = other.get("/episodes").get_data(as_text=True)
    check("stranger sees no delete form",
          f"/episodes/{slug}/clips/{cid}/delete" not in html2)
    html3 = me.get(f"/episodes/{slug}").get_data(as_text=True)
    check("owner sees delete form on watch page",
          f"/episodes/{slug}/clips/{cid}/delete" in html3)

    print("== clip delete: auth ==")
    r = me.post(f"/episodes/{slug}/clips/{cid}/delete",
                data={"csrf_token": tok}, environ_base=fresh_ip())
    check("owner delete redirects", r.status_code in (301, 302, 303),
          r.status_code)
    check("redirect lands on episode anchor",
          f"/episodes#{slug}" in r.headers.get("Location", ""),
          r.headers.get("Location", ""))
    check("clip row gone", appmod.db.clip(cid) is None)

    cid2 = make_clip(me, slug, "ClipOwner", start=10, end=20)
    r = other.post(f"/episodes/{slug}/clips/{cid2}/delete",
                   data={"csrf_token": tok2}, environ_base=fresh_ip())
    check("stranger delete -> 403", r.status_code == 403, r.status_code)
    check("clip survives stranger", appmod.db.clip(cid2) is not None)

    r = anon.post(f"/episodes/{slug}/clips/{cid2}/delete",
                  data={"csrf_token": "whatever"}, environ_base=fresh_ip())
    check("anon delete -> login redirect",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          (r.status_code, r.headers.get("Location", "")))
    check("clip survives anon", appmod.db.clip(cid2) is not None)

    r = me.post(f"/episodes/{slug}/clips/{cid2}/delete",
                data={"csrf_token": "wrong-token"}, environ_base=fresh_ip())
    check("bad csrf -> 403", r.status_code == 403, r.status_code)
    check("clip survives bad csrf", appmod.db.clip(cid2) is not None)

    r = me.post(f"/episodes/{slug}/clips/999999/delete",
                data={"csrf_token": tok}, environ_base=fresh_ip())
    check("unknown clip -> 404", r.status_code == 404, r.status_code)

    # case-insensitive owner match
    cid3 = make_clip(me, slug, "clipowner", start=20, end=30)
    r = me.post(f"/episodes/{slug}/clips/{cid3}/delete",
                data={"csrf_token": tok}, environ_base=fresh_ip())
    check("owner match is case-insensitive",
          r.status_code in (301, 302, 303) and
          appmod.db.clip(cid3) is None, r.status_code)

    print("== header handle (u/anon bug) ==")
    # logged in, no ts_handle cookie anywhere near this client
    html = me.get("/episodes").get_data(as_text=True)
    check("header chip shows session handle", "u/ClipOwner" in html)
    check("header chip never u/anon when logged in", "u/anon" not in html)
    html = other.get("/episodes").get_data(as_text=True)
    check("second user sees own handle", "u/ClipStranger" in html and
          "u/anon" not in html)
    html = anon.get("/episodes").get_data(as_text=True)
    check("logged-out, no cookie: no u/anon chip", "u/anon" not in html)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
