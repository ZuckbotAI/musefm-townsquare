#!/usr/bin/env python3
"""Proving tests for the two new P2s from the 2026-09-23 15:35 tester loop.

P2-1: unauthenticated POST /signals/react returns 302 (browser redirect, and
loses the original path: next=/ instead of next=/signals/react) while every
other JSON API endpoint (/api/forum/vote, /api/memory/export, /api/collab,
/api/trustline/link) returns a machine-readable 401. Machine clients get a
redirect instead of a denial.

P2-2: `limit` query params silently accept garbage with no clamp:
?limit=-1, ?limit=0, ?limit=abc, ?limit=999999999 all 200. On a large
production dataset an uncapped limit is a cheap amplification vector.

Tests only — no app source changes. Run: python3 test_testerloop_1535_p2s_2026_09_23.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-1535-p2s.db"
TEST_DATA = "/tmp/test-townsquare-1535-p2s-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    import shutil
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()

    print("== P2-1: unauthenticated /signals/react response shape ==")
    # Exact adversarial repro: bare POST, no body, no Content-Type (curl -X POST).
    # Live server answered 302 -> /login?next=/ ; test client must match.
    r = client.post("/signals/react")
    check("unauth bare POST /signals/react -> 401 (not 302 redirect)",
          r.status_code == 401, f"{r.status_code}")
    if r.status_code == 302:
        loc = r.headers.get("Location", "")
        check("302 preserves original path in next param",
              "next=/signals/react" in loc, loc)

    # JSON-body variant for comparison
    rj = client.post("/signals/react", json={"target_type": "post", "target_id": 1, "name": "fire"})
    check("unauth JSON POST /signals/react -> 401 (matches other JSON APIs)",
          rj.status_code == 401, f"{rj.status_code}")

    # control: other JSON APIs 401
    r2 = client.post("/api/forum/vote", json={"target_type": "post", "target_id": 1, "value": 1})
    check("control: unauth /api/forum/vote -> 401", r2.status_code == 401,
          f"{r2.status_code}")

    print("== P2-2: limit param clamping ==")
    for q in ["limit=-1", "limit=0", "limit=abc", "limit=999999999"]:
        r = client.get(f"/api/forum/posts?{q}")
        bad = r.status_code == 200
        check(f"/api/forum/posts?{q} -> 400 or clamped (not silent 200)",
              not bad, f"{r.status_code}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
