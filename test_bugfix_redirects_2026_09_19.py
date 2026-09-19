#!/usr/bin/env python3
"""Bug-fix proof 2026-09-19: /episodes/ and /townsquare redirects + Forum href.

P2s (tester loop 15:35 run):
- GET /episodes/ -> 404 (only /episodes existed); now 308 -> /episodes.
- /townsquare had no route (only /lobby and /forum did); now 301 -> /c/lobby.
- The sidebar Forum control rendered href="/"; now href="/c/lobby".

Run: python3 test_bugfix_redirects_2026_09_19.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-redirects.db"


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    r = c.get("/episodes/")
    print("  /episodes/ -> %d -> %s" % (r.status_code, r.headers.get("Location")))
    assert r.status_code == 308, r.status_code
    assert r.headers.get("Location") == "/episodes", r.headers.get("Location")
    assert c.get("/episodes").status_code == 200

    for path in ("/townsquare", "/lobby", "/forum"):
        r = c.get(path)
        print("  %s -> %d -> %s" % (path, r.status_code,
                                    r.headers.get("Location")))
        assert r.status_code == 301, (path, r.status_code)
        assert r.headers.get("Location") == "/c/lobby", r.headers.get("Location")

    r = c.get("/")
    html = r.get_data(as_text=True)
    assert 'href="/c/lobby"' in html, "Forum sidebar link missing /c/lobby href"
    # the link's visible label still says Forum
    assert ">Forum<" in html or ">Forum <" in html or "Forum" in html
    print("  home sidebar Forum control -> href=\"/c/lobby\"")
    print("PASS: redirects + Forum href correct")


if __name__ == "__main__":
    main()
