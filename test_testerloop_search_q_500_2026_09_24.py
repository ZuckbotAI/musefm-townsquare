#!/usr/bin/env python3
"""
Proving test for the NEW P1 found in the 2026-09-24 15:35 tester loop
(adversarial QA, verified live on the scratch instance + traceback in
server.log):

GET /api/forum/posts?q=<60000-char string> -> HTTP 500
  sqlite3.OperationalError: LIKE or GLOB pattern too complex
  (db.py:817 _q <- db.py:991 list_posts <- app.py:2571 api_posts)

Any unauthenticated client can 500 a public read endpoint with one
request. Sibling endpoints (/api/shorts?q=, /api/episodes?q=) return
200 for the same input, so the flaw is specific to forum post search:
the `q` string is interpolated into a LIKE pattern with no length cap.

EXPECTED: 200 with results, or a clean 400 "query too long".
TODAY:    500 with an uncaught sqlite3.OperationalError.

Tests only — no app source changes. Expected to FAIL on the current
checkout.

Run:  python3 -m pytest test_testerloop_search_q_500_2026_09_24.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-tl241535-searchq.db"


def _client():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)  # full schema + seeds, mirrors app startup
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def test_long_search_q_does_not_500():
    c = _client()
    r = c.get("/api/forum/posts", query_string={"q": "A" * 60000})
    assert r.status_code != 500, "HTTP 500 on long search query"
    assert r.status_code in (200, 400), (r.status_code, r.get_data(as_text=True)[:200])


def test_search_q_control_short_query_200():
    c = _client()
    r = c.get("/api/forum/posts", query_string={"q": "A" * 1000})
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:200])


if __name__ == "__main__":
    sys.exit(0)
