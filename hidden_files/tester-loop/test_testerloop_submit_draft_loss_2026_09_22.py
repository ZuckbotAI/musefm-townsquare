#!/usr/bin/env python3
"""Failing-test proof 2026-09-22 (tester loop 21:35 run, human persona):

P2 — a failed /submit discards the user's draft. POST /submit with an
invalid title (validation error -> 400) or when the 5/hr post budget is
spent (429) re-renders the composer with EMPTY pre_title/pre_body, so a
user who wrote a long post and trips validation or the rate limit loses
everything. Expected: the 400/429 re-render preserves the submitted
title and body in the composer (same UX as validation errors on other
forms).

Run: python3 hidden_files/tester-loop/test_testerloop_submit_draft_loss_2026_09_22.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/../..")

import app as appmod

TEST_DB = "/tmp/test-townsquare-submit-draft-loss-0922.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def csrf_of(client, ip):
    html = client.get("/submit", environ_base=ip).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf_token on /submit"
    return m.group(1)


def login_client():
    c = appmod.app.test_client()
    ip = {"REMOTE_ADDR": "10.77.0.31"}
    r = c.post("/signup", data={
        "handle": "draftkeeper", "password": "supersecret1",
        "password_confirm": "supersecret1", "display_name": "x",
        "bio": "x"}, environ_base=ip)
    assert r.status_code == 200, f"signup -> {r.status_code}"
    r = c.post("/login", data={
        "handle": "draftkeeper", "password": "supersecret1"},
        environ_base=ip)
    assert r.status_code == 302, f"login -> {r.status_code}"
    return c, ip


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True

    # ---- 400 path: 201-char title must not wipe the draft ----
    c, ip = login_client()
    tok = csrf_of(c, ip)
    title = "t" * 201
    body = "my precious draft body, several sentences long"
    r = c.post("/submit", data={
        "title": title, "body": body, "community": "lobby",
        "csrf_token": tok}, environ_base=ip)
    check("overlong title -> 400", r.status_code == 400,
          f"got {r.status_code}")
    html = r.get_data(as_text=True)
    check("400 re-render keeps the draft title",
          title in html, "submitted title missing from re-rendered composer")
    check("400 re-render keeps the draft body",
          "my precious draft body" in html,
          "submitted body missing from re-rendered composer")

    # ---- 400 path: whitespace-only title must not wipe the draft ----
    tok = csrf_of(c, ip)
    r = c.post("/submit", data={
        "title": "   ", "body": "second draft body", "community": "lobby",
        "csrf_token": tok}, environ_base=ip)
    check("whitespace-only title -> 400", r.status_code == 400,
          f"got {r.status_code}")
    html = r.get_data(as_text=True)
    check("whitespace-title 400 re-render keeps the draft body",
          "second draft body" in html,
          "submitted body missing from re-rendered composer")

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed" +
          (f": {FAIL}" if FAIL else ""))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
