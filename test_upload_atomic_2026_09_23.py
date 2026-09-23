"""Regression test: upload must not leave an orphan DB row when the file
cannot be stored (P0 2026-09-23 -- 15 seeded shorts 404'd because
create_video_upload INSERTed before writing the file, and the disk was
full). Run: python3 test_upload_atomic_2026_09_23.py
"""
import base64
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import videos
import ai_images

TEST_DB = "/tmp/test-townsquare-atomic.db"
TEST_DATA = "/tmp/test-townsquare-atomic-data"
UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def make_mp4(n=5000):
    return (b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00" * 16 +
            b"\x00\x00\x00\x08" + b"moov" + bytes(n))


def make_png():
    return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    from db import Database
    db = Database(TEST_DB)
    videos.ensure_video_schema(db)
    ai_images.ensure_ai_schema(db)
    return db


def row_count(db, table):
    r = db._one("SELECT COUNT(*) c FROM %s" % table)
    return r["c"] if r else 0


def main():
    db = setup()

    # 1. Happy path: file lands on disk AND row has stored_path set.
    uid, stored = videos.create_video_upload(
        db, "fm_test", "Tester", "a.mp4", make_mp4(), UPLOAD_DIR,
        ai_generated=True, duration_secs=6, title="t", status="approved")
    full = os.path.join(UPLOAD_DIR, os.path.basename(stored))
    check("video happy path: file on disk", os.path.isfile(full))
    check("video happy path: stored_path set",
          videos.get_video_upload(db, uid)["stored_path"] == stored, stored)
    check("video happy path: no tmp files left",
          not [f for f in os.listdir(UPLOAD_DIR) if "-tmp-" in f],
          str(os.listdir(UPLOAD_DIR)))

    # 2. Simulated full disk: pre-check must reject BEFORE any DB row.
    real_disk_usage = shutil.disk_usage

    class FakeUsage:
        free = 0
        total = 1
        used = 1

    shutil.disk_usage = lambda p: FakeUsage()
    before = row_count(db, "video_uploads")
    raised = None
    try:
        videos.create_video_upload(db, "fm_test", "Tester", "b.mp4",
                                   make_mp4(), UPLOAD_DIR, ai_generated=True,
                                   duration_secs=6)
    except ValueError as e:
        raised = str(e)
    finally:
        shutil.disk_usage = real_disk_usage
    check("video full disk: raises ValueError", raised is not None, str(raised))
    check("video full disk: no orphan row",
          row_count(db, "video_uploads") == before,
          "rows before=%d" % before)

    # 3. Simulated write failure mid-way (OSError on write): no orphan row.
    real_open = open

    def boom_open(*a, **k):
        raise OSError(28, "No space left on device")

    import builtins
    builtins.open = boom_open
    before = row_count(db, "video_uploads")
    raised = None
    try:
        videos.create_video_upload(db, "fm_test", "Tester", "c.mp4",
                                   make_mp4(), UPLOAD_DIR, ai_generated=True,
                                   duration_secs=6)
    except ValueError as e:
        raised = str(e)
    finally:
        builtins.open = real_open
    check("video write OSError: raises ValueError", raised is not None)
    check("video write OSError: no orphan row",
          row_count(db, "video_uploads") == before)

    # 4. Same guarantees for images.
    iid, istored = ai_images.create_image_upload(
        db, "fm_test", "Tester", "a.png", make_png(), UPLOAD_DIR,
        ai_generated=True, status="approved")
    ifull = os.path.join(UPLOAD_DIR, os.path.basename(istored))
    check("image happy path: file on disk", os.path.isfile(ifull))
    shutil.disk_usage = lambda p: FakeUsage()
    before = row_count(db, "ai_uploads")
    raised = None
    try:
        ai_images.create_image_upload(db, "fm_test", "Tester", "b.png",
                                      make_png(), UPLOAD_DIR, ai_generated=True)
    except ValueError as e:
        raised = str(e)
    finally:
        shutil.disk_usage = real_disk_usage
    check("image full disk: raises ValueError", raised is not None)
    check("image full disk: no orphan row",
          row_count(db, "ai_uploads") == before)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
