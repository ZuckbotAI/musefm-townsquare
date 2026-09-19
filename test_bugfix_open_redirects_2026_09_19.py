#!/usr/bin/env python3
"""Bug-fix proof 2026-09-19: open redirects stay closed.

P2 (tester loop 15:35 run) — verified closed in the report; this is the
standing regression. Every user-controlled redirect target in app.py goes
through _safe_next(), which only allows plain in-site paths.

Covers: absolute https://evil URLs, scheme-relative //evil, javascript:,
backslash tricks, and CR/LF/TAB/NUL injections — at the unit level and
through a real login POST (the highest-traffic `next` consumer).

Run: python3 test_bugfix_open_redirects_2026_09_19.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-open-redirects.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


EVIL = [
    "https://evil.example.com/",
    "http://evil.example.com/phish",
    "//evil.example.com/",
    "///evil.example.com/",
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "/\\evil.example.com",
    "\\/evil.example.com",
    "/\\\n/evil.example.com",
    "/foo\nevil.example.com",
    "/foo\revil.example.com",
    "/foo\tevil.example.com",
    "/foo\x00evil.example.com",
    "https:evil.example.com",
]


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True

    # unit level: _safe_next must never return an off-site target
    for v in EVIL:
        out = appmod._safe_next(v)
        check("_safe_next(%r) -> default" % v[:40],
              out == "/" and "evil" not in out, out)
    # legit in-site targets still pass through untouched
    for v in ["/c/lobby", "/c/lobby?x=1", "/submit", "/m/Zuckbot",
              "/episodes#ep01"]:
        check("_safe_next(%r) passthrough" % v,
              appmod._safe_next(v) == v, appmod._safe_next(v))

    # route level: login POST with an evil `next` must land on-site
    c = appmod.app.test_client()
    r = c.post("/signup", data={"handle": "RedirHuman",
                                "password": "supersecret1",
                                "password_confirm": "supersecret1"})
    assert r.status_code == 200, r.get_data(as_text=True)[:160]
    for v in ["https://evil.example.com/", "//evil.example.com/",
              "javascript:alert(1)", "/\\evil.example.com"]:
        r = c.post("/login", data={"handle": "RedirHuman",
                                   "password": "supersecret1",
                                   "next": v})
        loc = r.headers.get("Location", "")
        check("login next=%r -> on-site (%r)" % (v[:30], loc[:40]),
              r.status_code == 302 and
              (loc.startswith("/") and "evil" not in loc),
              "%d -> %r" % (r.status_code, loc))
    # a legit next still works
    r = c.post("/login", data={"handle": "RedirHuman",
                               "password": "supersecret1",
                               "next": "/c/lobby"})
    check("login next=/c/lobby honored",
          r.status_code == 302 and
          r.headers.get("Location") == "/c/lobby",
          r.headers.get("Location"))

    print("== %d PASS, %d FAIL ==" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
