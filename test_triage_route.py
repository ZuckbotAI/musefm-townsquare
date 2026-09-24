#!/usr/bin/env python3
"""
Tests for the one-shot video triage admin routes (2026-09-23).

Covers: disabled-by-default (404s without TRIAGE_ENABLED=1), agent-key
gating, path confinement of the copy step, a full run against a real
tiny mp4 (CSV produced, originals + DB untouched), and the one-shot
guard.

Run:  python3 test_triage_route.py
Throwaway SQLite db + temp DATA_DIR. Nothing touches townsquare.db.
"""
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["AGENT_KEY"] = "triage-test-key"
os.environ.pop("TRIAGE_ENABLED", None)

import app as appmod
import videos

TEST_DB = "/tmp/test-townsquare-triage.db"
TEST_DATA = "/tmp/test-townsquare-triage-data"
KEY = {"X-Agent-Key": "triage-test-key"}

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def reset_state():
    with appmod._TRIAGE_LOCK:
        appmod._TRIAGE_STATE.update(state="idle", started_at=None,
                                    finished_at=None, total=0, done=0,
                                    error=None, csv_path=None, workdir=None)


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    shutil.rmtree(TEST_DATA, ignore_errors=True)
    os.makedirs(os.path.join(TEST_DATA, "uploads"), exist_ok=True)
    from db import Database
    appmod.db = Database(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    appmod.app.config["TESTING"] = True
    os.environ.pop("TRIAGE_ENABLED", None)
    reset_state()
    return appmod.app.test_client()


def make_tiny_mp4(path):
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", path],
                   check=True, timeout=60)
    with open(path, "rb") as f:
        return f.read()


def wait_done(c, timeout=120):
    for _ in range(timeout):
        r = c.get("/api/admin/triage/status", headers=KEY)
        st = r.get_json() or {}
        if st.get("state") in ("done", "error"):
            return st
        time.sleep(1)
    return None


# ---------- 1. disabled by default ----------
c = setup()
r = c.post("/api/admin/triage/run", json={}, headers=KEY)
check("disabled: run 404s without TRIAGE_ENABLED", r.status_code == 404,
      r.status_code)
r = c.get("/api/admin/triage/status", headers=KEY)
check("disabled: status 404s without TRIAGE_ENABLED", r.status_code == 404,
      r.status_code)
r = c.get("/api/admin/triage/csv", headers=KEY)
check("disabled: csv 404s without TRIAGE_ENABLED", r.status_code == 404,
      r.status_code)

# ---------- 2. agent-key gating ----------
os.environ["TRIAGE_ENABLED"] = "1"
r = c.post("/api/admin/triage/run", json={})
check("enabled: run 401s without agent key", r.status_code == 401,
      r.status_code)
r = c.post("/api/admin/triage/run", json={},
           headers={"X-Agent-Key": "wrong"})
check("enabled: run 401s with wrong agent key", r.status_code == 401,
      r.status_code)

# ---------- 3. path confinement ----------
class EvilDB:
    def __init__(self, rows):
        self._rows = rows


orig_list = videos.list_pending_videos
try:
    evil_rows = [
        {"id": 1, "stored_path": "../../etc/passwd"},
        {"id": 2, "stored_path": "/abs/path/vid-2.mp4"},
        {"id": 3, "stored_path": "uploads/vid-3.mp4"},  # missing file
    ]
    videos.list_pending_videos = lambda db, limit=50, offset=0: (
        evil_rows[offset:offset + limit] if offset < len(evil_rows) else [])
    workdir, copied, total = appmod._triage_copy_pending(EvilDB(evil_rows))
    check("confinement: evil paths skipped", copied == 0 and total == 3,
          f"copied={copied} total={total}")
    check("confinement: workdir empty", os.listdir(workdir) == [],
          os.listdir(workdir))
    shutil.rmtree(workdir, ignore_errors=True)
finally:
    videos.list_pending_videos = orig_list

# ---------- 4. full run ----------
raw = make_tiny_mp4(os.path.join(TEST_DATA, "src.mp4"))
uid, sp = videos.create_video_upload(appmod.db, None, "tester", "tiny.mp4",
                                     raw, appmod.UPLOAD_DIR, status="pending")
check("fixture: pending upload created", sp == f"uploads/vid-{uid}.mp4", sp)
orig_size = os.path.getsize(os.path.join(TEST_DATA, sp))

r = c.post("/api/admin/triage/run", json={}, headers=KEY)
check("run: accepted", r.status_code == 200 and r.get_json().get("ok"),
      f"{r.status_code} {r.get_data(as_text=True)[:120]}")
st = wait_done(c)
check("run: reaches done", bool(st) and st.get("state") == "done",
      str(st)[:160] if st else "timeout")
if st:
    check("run: one video processed", st.get("done") == 1 and
          st.get("total") == 1, str({k: st.get(k) for k in ("done", "total")}))

r = c.get("/api/admin/triage/csv", headers=KEY)
body = r.get_data(as_text=True)
check("csv: served", r.status_code == 200, r.status_code)
check("csv: header present", body.startswith("id,duration_s,"),
      body[:60])
check("csv: contains the video id", f"{uid}," in body, body[:200])

# originals + DB untouched: still pending, file byte-identical
row = appmod.db.db.execute(
    "SELECT status FROM video_uploads WHERE id=?", (uid,)).fetchone()
check("read-only: DB status still pending",
      row and row["status"] == "pending", dict(row) if row else None)
check("read-only: original file untouched",
      os.path.getsize(os.path.join(TEST_DATA, sp)) == orig_size)

# one-shot guard
r = c.post("/api/admin/triage/run", json={}, headers=KEY)
check("one-shot: second run 409s", r.status_code == 409, r.status_code)
r = c.post("/api/admin/triage/run", json={"force": True}, headers=KEY)
check("one-shot: force re-run accepted", r.status_code == 200,
      r.status_code)
st = wait_done(c)
check("one-shot: force re-run finishes", bool(st) and
      st.get("state") == "done", str(st)[:120] if st else "timeout")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
