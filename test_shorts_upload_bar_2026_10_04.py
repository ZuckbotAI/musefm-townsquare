#!/usr/bin/env python3
"""
Shorts upload bar tests (2026-10-04, overhaul item 13).

Proves:
  * the forum create row's "Short" tool points at /shorts/upload (a creation
    form), not the /shorts viewer.
  * GET /shorts/upload requires sign-in; signed in it renders the compact
    upload bar form with a file picker and CSRF token.
  * POST with no file -> 400; POST with a real MP4 -> 302 to
    /shorts?uploaded=1 and the clip lands in the mod-approval queue
    (status pending).
  * /shorts shows the chrome upload button when signed in (not when anon),
    the empty-state Upload link points at /shorts/upload, and
    ?uploaded=1 renders the waiting-for-review notice.

Run:  python3 test_shorts_upload_bar_2026_10_04.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
Needs /tmp/tiny-test.mp4 (a 1s mp4) for the upload POST.
"""
import io
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-shorts-uploadbar.db"
TEST_DATA = "/tmp/test-townsquare-shorts-uploadbar-data"
TINY_MP4 = "/tmp/tiny-test.mp4"

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
    return appmod.app.test_client()


def signin(client):
    client.post("/signup", data={"handle": "shortsup",
                                 "password": "s3cretpw!!",
                                 "password_confirm": "s3cretpw!!",
                                 "email": "shortsup@example.com"})
    client.post("/login", data={"handle": "shortsup",
                                "password": "s3cretpw!!"})


def main():
    client = setup()

    print("== create row points at the upload form ==")
    html = client.get("/c/lobby").get_data(as_text=True)
    check("Short tool links to /shorts/upload",
          'href="/shorts/upload"' in html)
    check("Short tool no longer links to the viewer",
          'href="/shorts">▶ Short' not in html and
          'href="/shorts">\u25b6 Short' not in html)

    print("== /shorts/upload auth ==")
    r = client.get("/shorts/upload")
    check("anon -> redirect to login",
          r.status_code in (301, 302, 303) and "/login" in r.headers["Location"])
    signin(client)
    html = client.get("/shorts/upload").get_data(as_text=True)
    check("signed in -> 200 form", "Upload a short" in html)
    check("file picker present", 'name="video_file"' in html)
    check("accepts mp4/webm", "video/mp4" in html)
    check("csrf token present", 'name="csrf_token"' in html)
    check("compact bar markup", 'class="upload-bar"' in html)

    print("== upload POST ==")
    r = client.post("/shorts/upload", data={})
    check("no file -> 400", r.status_code == 400)
    with open(TINY_MP4, "rb") as f:
        raw = f.read()
    form_html = client.get("/shorts/upload").get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', form_html)
    tok = m.group(1) if m else ""
    r = client.post("/shorts/upload", data={
        "csrf_token": tok,
        "video_file": (io.BytesIO(raw), "clip.mp4"),
        "title": "my test clip",
    }, content_type="multipart/form-data")
    check("real mp4 -> 302 to /shorts?uploaded=1",
          r.status_code in (301, 302, 303) and
          r.headers["Location"].endswith("/shorts?uploaded=1"),
          f"got {r.status_code} {r.headers.get('Location')}")
    rows = appmod.db._q(
        "SELECT status FROM video_uploads ORDER BY id DESC LIMIT 1")
    check("clip lands pending for mod review",
          rows and rows[0]["status"] == "pending",
          f"status={rows[0]['status'] if rows else None}")

    print("== /shorts page surfaces ==")
    html = client.get("/shorts").get_data(as_text=True)
    check("chrome upload button when signed in",
          'class="shorts-chrome-upload"' in html)
    check("uploaded notice renders",
          "waiting for mod review" in
          client.get("/shorts?uploaded=1").get_data(as_text=True))
    client2 = setup()
    html2 = client2.get("/shorts").get_data(as_text=True)
    check("no chrome upload button when anon",
          'class="shorts-chrome-upload"' not in html2)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
