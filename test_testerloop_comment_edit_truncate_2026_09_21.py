#!/usr/bin/env python3
"""
tester-loop 2026-09-21 22:35: proves the /comment/edit silent-truncation P1
is FIXED.

Repro: log in as a human, edit one of your own comments with a 3000-char
body via POST /comment/edit.

Bug (before fix): 302 success, body stored cut to 2000 chars, tail silently
lost -- db.edit_comment did clean(body, 2000) with no error. Same root cause
as the known comment-CREATE truncation P1, but the edit path was never
tested.

Fixed behavior: oversize bodies are REJECTED with a clear 400 ("comment body
too long — max 2000 characters") on both form and JSON paths, and the stored
body is left untouched. Exactly-2000-char bodies still go through.
db.clean_comment_body() replaces the truncating clean(body, 2000) on all
four comment paths (create/edit x forum/video/episode).

Run: python3 test_testerloop_comment_edit_truncate_2026_09_21.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-comment-edit-truncate-2026-09-21.db"
TEST_DATA = "/tmp/test-townsquare-comment-edit-truncate-2026-09-21-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    db = appmod.db

    client = appmod.app.test_client()
    r = client.post("/signup", data={"handle": "EditTrunc",
                                     "password": "supersecret1",
                                     "password_confirm": "supersecret1"},
                    environ_base={"REMOTE_ADDR": "10.201.0.1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    r = client.post("/login", data={"handle": "EditTrunc",
                                    "password": "supersecret1"},
                    environ_base={"REMOTE_ADDR": "10.201.0.2"})
    assert r.status_code == 302, r.get_data(as_text=True)
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in human"
    tok = m.group(1)

    pid = db.create_post("lobby", "EditTrunc", "truncate probe",
                         "probe body", "discussion")
    cid = db.create_comment(pid, None, "EditTrunc", "initial comment")

    # --- edit path: oversize rejected, nothing stored, clear message ---
    big = "x" * 3000
    r = client.post("/comment/edit",
                    data={"csrf_token": tok, "target_type": "comment",
                          "target_id": cid, "body": big},
                    environ_base={"REMOTE_ADDR": "10.201.0.3"})
    body_txt = r.get_data(as_text=True)
    check("edit of 3000-char body rejected (400, not silent 302)",
          r.status_code == 400, "got %d" % r.status_code)
    check("rejection message is clear (mentions too long / 2000)",
          "too long" in body_txt and "2000" in body_txt,
          body_txt[:120])

    row = db._one("SELECT body FROM comments WHERE id=?", (cid,))
    stored = row["body"] if row else ""
    check("rejected edit leaves stored body untouched",
          stored == "initial comment", repr(stored)[:80])

    # --- edit path: exactly 2000 chars still accepted ---
    edge = "y" * 2000
    r = client.post("/comment/edit",
                    data={"csrf_token": tok, "target_type": "comment",
                          "target_id": cid, "body": edge},
                    environ_base={"REMOTE_ADDR": "10.201.0.3"})
    check("edit of exactly-2000-char body accepted (302)",
          r.status_code == 302, "got %d" % r.status_code)
    row = db._one("SELECT body FROM comments WHERE id=?", (cid,))
    stored = row["body"] if row else ""
    check("2000-char edit stored at full length",
          len(stored) == 2000, "stored len=%d" % len(stored))

    # --- JSON path: same rejection ---
    r = client.post("/comment/edit",
                    json={"csrf_token": tok, "target_type": "comment",
                          "target_id": cid, "body": "z" * 2001},
                    environ_base={"REMOTE_ADDR": "10.201.0.4"})
    j = r.get_json() or {}
    check("JSON edit of 2001-char body -> 400 with clear error",
          r.status_code == 400 and "too long" in (j.get("error") or ""),
          str(j)[:120])

    # --- create path: oversize raises, nothing stored ---
    try:
        db.create_comment(pid, None, "EditTrunc", "w" * 2500)
        created = True
    except ValueError as e:
        created = False
        msg = str(e)
    check("create of 2500-char comment raises ValueError", not created)
    check("create rejection message is clear",
          (not created) and "too long" in msg and "2000" in msg,
          msg if not created else "")
    n = db._one("SELECT COUNT(*) c FROM comments WHERE post_id=?", (pid,))["c"]
    check("oversize create stored nothing", n == 1, "count=%d" % n)

    # --- create path: exactly 2000 still works ---
    cid2 = db.create_comment(pid, None, "EditTrunc", "v" * 2000)
    row = db._one("SELECT body FROM comments WHERE id=?", (cid2,))
    check("create of exactly-2000-char comment stored at full length",
          row and len(row["body"]) == 2000)

    # --- video + episode comment paths reject too ---
    import time as _t
    db._exec("INSERT INTO video_uploads (handle, filename, stored_path,"
             " bytes, mime, created_at) VALUES (?,?,?,?,?,?)",
             ("EditTrunc", "v.mp4", "v.mp4", 10, "video/mp4",
              int(_t.time())))
    vid = db._one("SELECT id FROM video_uploads")["id"]
    try:
        db.create_video_comment(vid, None, "EditTrunc", "q" * 2001)
        v_msg = None
    except ValueError as e:
        v_msg = str(e)
    check("video comment oversize raises with clear message",
          v_msg is not None and "too long" in v_msg, v_msg or "no raise")
    # valid-size video comment still works
    vcid = db.create_video_comment(vid, None, "EditTrunc", "q" * 2000)
    check("video comment at exactly 2000 chars accepted", bool(vcid))

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
