#!/usr/bin/env python3
"""Upload transcoding + poster frames + site-wide rotation (2026-10-04).

Proves the critical item 1 gaps are closed:
- a real MP4 with moov-at-end uploads through create_video_upload and
  comes out faststart (moov up front), playable (ffprobe reads it), with
  a JPEG poster frame served at /video/<uid>/poster and wired into the
  watch page's poster attribute.
- synthetic/non-video bytes still upload exactly as before (the ffmpeg
  pass is best-effort and never a rejection gate), with no poster.
- the rotating-videos algorithm applies site-wide: the /musefm hub
  shuffles with the shared recency memory (unseen-first), and /watch
  carries an Up next rotation slot that excludes the current clip.
- approving a pending upload notifies the uploader.

Throwaway SQLite db + temp DATA_DIR. Nothing touches townsquare.db.
Requires ffmpeg on PATH (skips the transcode assertions if missing).
"""
import io
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "tss", os.path.join(_here, "test_shorts_shuffle.py"))
tss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tss)

import app as appmod  # noqa: E402
import videos  # noqa: E402

TEST_DB = "/tmp/test-townsquare-upload-transcode.db"
TEST_DATA = "/tmp/test-townsquare-upload-transcode-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (" -- %s" % detail if detail and not cond else ""))


def make_real_mp4(path, secs=2):
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i",
         "testsrc=duration=%d:size=320x240:rate=10" % secs,
         "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", path],
        capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode()[:300]
    with open(path, "rb") as fh:
        return fh.read()


def moov_before_mdat(raw):
    moov = raw.find(b"moov")
    mdat = raw.find(b"mdat")
    return moov != -1 and mdat != -1 and moov < mdat


def ffprobe_ok(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, timeout=60)
    return r.returncode == 0


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database, ensure_human_auth_schema
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    videos.ensure_video_schema(appmod.db)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def main():
    client = setup()
    has_ffmpeg = bool(shutil.which("ffmpeg"))

    print("== transcode + poster on a real upload ==")
    raw = make_real_mp4("/tmp/transcode-in.mp4")
    check("test input really has moov at the end", not moov_before_mdat(raw))
    priv, fm = tss.register(client, "Transcoder")
    uid, stored = videos.create_video_upload(
        appmod.db, fm, "Transcoder", "clip.mp4", raw, appmod.UPLOAD_DIR,
        ai_generated=True, duration_secs=30, status="approved")
    full = os.path.join(appmod.DATA_DIR, stored)
    out = open(full, "rb").read()
    check("stored file exists", os.path.isfile(full))
    if has_ffmpeg:
        check("faststart applied (moov before mdat)", moov_before_mdat(out),
              "moov=%d mdat=%d" % (out.find(b"moov"), out.find(b"mdat")))
        check("stored file is playable", ffprobe_ok(full))
        u = videos.get_video_upload(appmod.db, uid)
        check("poster_path recorded", bool(u.get("poster_path")),
              u.get("poster_path"))
        pfull = os.path.join(appmod.DATA_DIR, u.get("poster_path") or "")
        check("poster file exists", os.path.isfile(pfull))
        phead = open(pfull, "rb").read(3) if os.path.isfile(pfull) else b""
        check("poster is a JPEG", phead[:2] == b"\xff\xd8", phead[:2])
        r = client.get("/video/%d/poster" % uid)
        check("poster serves 200 image/jpeg",
              r.status_code == 200 and r.content_type == "image/jpeg",
              "%d %s" % (r.status_code, r.content_type))
        check("poster bytes match", r.data == open(pfull, "rb").read())
        r = client.get("/watch/%d" % uid)
        check("watch page carries the poster attribute",
              ('poster="/video/%d/poster"' % uid) in
              r.get_data(as_text=True))
    else:
        print("  (no ffmpeg: transcode assertions skipped)")

    print("== best-effort fallback: synthetic bytes still upload ==")
    fake = tss.make_mp4()
    uid2, stored2 = videos.create_video_upload(
        appmod.db, fm, "Transcoder", "fake.mp4", fake, appmod.UPLOAD_DIR,
        ai_generated=True, duration_secs=30, status="approved")
    u2 = videos.get_video_upload(appmod.db, uid2)
    check("synthetic upload accepted", bool(uid2))
    check("no poster for non-video bytes", not u2.get("poster_path"),
          u2.get("poster_path"))
    check("bytes stored as-is",
          open(os.path.join(appmod.DATA_DIR, stored2), "rb").read() == fake)
    r = client.get("/video/%d/poster" % uid2)
    check("missing poster -> 404", r.status_code == 404, r.status_code)

    print("== rotation is site-wide: /musefm hub ==")
    pool = []
    for n in range(8):
        p2, f2 = tss.register(client, "Hubber%d" % n)
        v = tss.post_video(client, p2, f2, tss.make_mp4())
        videos.set_series(appmod.db, v, "musefm")
        pool.append(v)
    c1 = appmod.app.test_client()
    h1 = c1.get("/musefm").get_data(as_text=True)
    s1 = re.findall(r"series=musefm(?:&amp;|&)video=(\d+)", h1)
    # Same visitor (same session cookie): the recency window must push the
    # two clips the first load missed to the front of the second load.
    h2 = c1.get("/musefm").get_data(as_text=True)
    s2 = re.findall(r"series=musefm(?:&amp;|&)video=(\d+)", h2)
    check("hub renders 6 station clips", len(s1) == 6 and len(s2) == 6,
          "%d/%d" % (len(s1), len(s2)))
    missed = set(str(i) for i in pool) - set(s1)
    check("second hub load leads with unseen clips",
          set(s2[:len(missed)]) == missed and not (set(s1) & set(s2[:2]))
          if missed else True,
          "s1=%s s2=%s" % (s1, s2))

    print("== rotation is site-wide: /watch up-next ==")
    r = c1.get("/watch/%d" % pool[0])
    html = r.get_data(as_text=True)
    check("watch page renders", r.status_code == 200, r.status_code)
    m = re.search(r"Up next.*", html, re.S)
    up = m.group(0) if m else ""
    up_ids = re.findall(r"/watch/(\d+)", up)
    check("up-next section lists other clips", len(up_ids) > 0, up_ids)
    check("up-next excludes the current clip",
          str(pool[0]) not in up_ids, up_ids)

    print("== uploader is notified on approval ==")
    p3, f3 = tss.register(client, "Notifyee")
    uid3, _ = videos.create_video_upload(
        appmod.db, f3, "Notifyee", "pending.mp4", tss.make_mp4(),
        appmod.UPLOAD_DIR, ai_generated=False, duration_secs=30,
        status="pending")
    before = len(appmod.db.notifications_for(f3, 50))
    videos.set_video_status(appmod.db, uid3, "approved")
    appmod._notify_upload_uploader("video", uid3, True)
    notes = appmod.db.notifications_for(f3, 50)
    check("approval notification delivered",
          len(notes) == before + 1 and notes[0]["type"] == "upload_approved",
          [n["type"] for n in notes[:3]])

    print()
    print("%d passed, %d failed" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
