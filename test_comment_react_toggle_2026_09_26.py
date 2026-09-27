#!/usr/bin/env python3
"""Proving test for the 2026-09-26 comment-react tap-toggle P1 fix
(Anthony's order: reimplement the toggle in the redesign worktree).

Bug: POST /comment/react twice with the same emoji returned "added"
both times; the second tap must return "removed" (toggle), matching the
comment_react_web docstring. Explicit action=remove must keep working.

Tests only. Run: python3 test_comment_react_toggle_2026_09_26.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-worktree-comment-react-toggle-2026-09-26.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    human = appmod.app.test_client()

    r = human.post("/signup", data={"handle": "ToggleFix", "email": "toggle@example.test",
                                    "password": "supersecret1",
                                    "password_confirm": "supersecret1"})
    assert r.status_code == 200, r.get_data(as_text=True)[:160]
    r = human.post("/login", data={"handle": "ToggleFix", "password": "supersecret1"})
    assert r.status_code == 302, r.get_data(as_text=True)[:160]
    html = human.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta"
    tok = m.group(1)

    rp = human.post("/submit", data={"community": "lobby", "title": "toggle t",
                                     "body": "b", "csrf_token": tok})
    assert rp.status_code in (302, 303), rp.status_code
    loc = rp.headers.get("Location", "/")
    mpid = re.search(r"/post/(\d+)", loc)
    assert mpid, loc
    pid = mpid.group(1)
    rc = human.post(f"/post/{pid}/comment",
                    data={"body": "react me", "csrf_token": tok})
    assert rc.status_code in (302, 303), rc.get_data(as_text=True)[:160]
    crow = appmod.db._one("SELECT id FROM comments WHERE post_id=? ORDER BY id DESC LIMIT 1",
                          (int(pid),))
    cid = crow["id"]
    emoji = "\U0001F680"

    def react(**extra):
        return human.post("/comment/react", json={"target_type": "comment",
                           "target_id": cid, "emoji": emoji,
                           "csrf_token": tok, **extra})

    j1 = react().get_json()
    check("first tap adds", j1 and j1.get("action") == "added", f"{j1}")
    j2 = react().get_json()
    check("second tap with same emoji removes (toggle)",
          j2 and j2.get("action") == "removed" and j2.get("total") == 0, f"{j2}")
    j3 = react().get_json()
    check("third tap re-adds", j3 and j3.get("action") == "added"
          and j3.get("total") == 1, f"{j3}")
    j4 = react(action="remove").get_json()
    check("explicit action=remove still removes",
          j4 and j4.get("action") == "removed" and j4.get("total") == 0, f"{j4}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
