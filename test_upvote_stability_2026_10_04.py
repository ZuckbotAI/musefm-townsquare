#!/usr/bin/env python3
"""Proving tests for critical item 2 (2026-10-04): no page jump on upvotes.

Two root causes, two fixes, all covered here:

A. Layout shift from media without declared dimensions. Intrinsic
   width/height are now probed at upload time (PIL for images/GIFs,
   ffprobe for videos), backfilled for legacy rows, and emitted as
   width/height attributes on feed media so the browser reserves the
   box before the bytes arrive.

B. The no-JS /vote fallback reloaded the page with no scroll anchor, so
   the page jumped to the top on every vote. /vote now redirects to
   next + #post-<id> (or #c<id> for comments), matching the DOM anchors
   added to community.html and post.html.

Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
Run:  python3 test_upvote_stability_2026_10_04.py
"""
import io
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

TEST_DB = "/tmp/test-townsquare-upvote-stability.db"
TEST_DATA = "/tmp/test-townsquare-upvote-stability-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def make_png(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (20, 40, 60)).save(buf, "PNG")
    return buf.getvalue()


def make_gif(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (60, 20, 40)).save(buf, "GIF")
    return buf.getvalue()


def make_mp4(path, w, h):
    """A real, playable 1s MP4 via ffmpeg. Returns True on success."""
    import shutil as _sh
    if not _sh.which("ffmpeg"):
        return False
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=%dx%d:rate=10:duration=1" % (w, h),
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", path],
        capture_output=True, timeout=60)
    return r.returncode == 0 and os.path.getsize(path) > 0


def main():
    for p in (TEST_DB, TEST_DATA):
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.exists(p):
            os.remove(p)
    import app as appmod
    import ai_images
    import videos
    import gifs

    appmod.db = appmod.init_db(TEST_DB)  # full boot schema, like production
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True
    db = appmod.db

    png = make_png(640, 480)
    gif = make_gif(320, 200)

    # --- A1: dims stored at upload time ---------------------------------
    uid_img, _ = ai_images.create_image_upload(
        db, None, "dimtester", "t.png", png, appmod.UPLOAD_DIR,
        status="approved")
    row = ai_images.get_image_upload(db, uid_img)
    check("image upload stores probed dims",
          (row["width"], row["height"]) == (640, 480),
          (row["width"], row["height"]))

    uid_gif, _ = gifs.create_gif_upload(
        db, None, "dimtester", "t.gif", gif, appmod.UPLOAD_DIR)
    grow = gifs.get_gif_upload(db, uid_gif)
    check("gif upload stores probed dims",
          (grow["width"], grow["height"]) == (320, 200),
          (grow["width"], grow["height"]))

    mp4_path = os.path.join(TEST_DATA, "probe-src.mp4")
    if make_mp4(mp4_path, 320, 240):
        with open(mp4_path, "rb") as f:
            raw = f.read()
        uid_vid, _ = videos.create_video_upload(
            db, None, "dimtester", "t.mp4", raw, appmod.UPLOAD_DIR,
            status="approved")
        vrow = videos.get_video_upload(db, uid_vid)
        check("video upload stores ffprobe dims",
              (vrow["width"], vrow["height"]) == (320, 240),
              (vrow["width"], vrow["height"]))
    else:
        print("  SKIP video upload dims (no ffmpeg on this box)")
        uid_vid = None

    # --- A2: probes never fail an upload --------------------------------
    check("image probe of garbage -> (None, None), no raise",
          ai_images.probe_image_dims(b"definitely not an image") ==
          (None, None))
    check("gif probe of garbage -> (None, None), no raise",
          gifs.probe_gif_dims(b"definitely not a gif") == (None, None))
    check("video probe of missing file -> (None, None), no raise",
          videos.probe_video_dims("/no/such/file.mp4") == (None, None))
    uid_bad, _ = ai_images.create_image_upload(
        db, None, "dimtester", "t2.png", png, appmod.UPLOAD_DIR,
        status="approved")
    db._exec("UPDATE ai_uploads SET width=NULL, height=NULL WHERE id=?",
             (uid_bad,))
    check("NULL dims accepted (legacy-shaped row)",
          ai_images.get_image_upload(db, uid_bad)["width"] is None)

    # --- A3: backfill heals legacy rows ---------------------------------
    n = ai_images.backfill_dimensions(db, appmod.UPLOAD_DIR)
    fixed = ai_images.get_image_upload(db, uid_bad)
    check("backfill fills NULL dims from the stored file",
          n >= 1 and (fixed["width"], fixed["height"]) == (640, 480),
          (n, fixed["width"], fixed["height"]))

    # --- A4: rendered thread HTML carries declared dimensions ------------
    db.register_identity("votertester", "k" * 43)
    fm = db._one("SELECT fm_id FROM identities WHERE handle='votertester'"
                 )["fm_id"]
    from app import _csrf_token
    with appmod.app.test_request_context():
        tok = _csrf_token()
    poster = appmod.app.test_client()
    with poster.session_transaction() as s:
        s["fm_id"] = fm
        s["session_epoch"] = (db.get_identity(fm) or {}).get(
            "session_epoch", 0)
        s["csrf_token"] = tok
    vid_url = ("/video/%d" % uid_vid) if uid_vid else ""
    pid = db.create_post("lobby", "votertester", "dims post", "body text",
                         "discussion", image_url="/img/%d" % uid_img,
                         video_url=vid_url, bypass_filter=True)
    r = poster.get("/c/lobby/post/%d" % pid)
    html = r.get_data(as_text=True)
    check("thread page 200", r.status_code == 200, r.status_code)
    m = re.search(r'<img class="post-img"[^>]*>', html)
    check("thread <img> carries width/height",
          bool(m and 'width="640"' in m.group(0)
               and 'height="480"' in m.group(0)),
          m.group(0)[:160] if m else "no post-img found")
    if uid_vid:
        mv = re.search(r'<video class="post-video"[^>]*>', html)
        check("thread <video> carries width/height",
              bool(mv and 'width="320"' in mv.group(0)
                   and 'height="240"' in mv.group(0)),
              mv.group(0)[:160] if mv else "no post-video found")
    check("thread post block has the vote scroll anchor",
          'id="post-%d"' % pid in html)
    r = poster.get("/c/lobby")
    check("forum feed 200", r.status_code == 200, r.status_code)
    check("forum card has the vote scroll anchor",
          'id="post-%d"' % pid in r.get_data(as_text=True))

    # --- A5: annotate helper --------------------------------------------
    items = [{"image_url": "/img/%d" % uid_img},
             {"image_url": "https://media.tenor.com/abc123.gif"},
             {"video_url": "/video/999999"}]
    appmod._annotate_media_dims(items)
    check("annotate sets image_w/h from the upload row",
          (items[0].get("image_w"), items[0].get("image_h")) == (640, 480),
          (items[0].get("image_w"), items[0].get("image_h")))
    check("annotate leaves external gif urls alone",
          "image_w" not in items[1])
    check("annotate leaves unknown uids alone",
          "video_w" not in items[2])

    # --- B1: no-JS post vote redirects to the post anchor ---------------
    r = poster.post("/vote", data={"target_type": "post",
                                   "target_id": str(pid),
                                   "value": "1",
                                   "csrf_token": tok,
                                   "next": "/c/lobby?sort=hot"})
    loc = r.headers.get("Location", "")
    check("form vote on post -> 302", r.status_code == 302, r.status_code)
    check("form vote redirect keeps the voter on the post anchor",
          loc == "/c/lobby?sort=hot#post-%d" % pid, loc)

    # --- B2: no-JS comment vote redirects to the comment anchor ---------
    cid = db.create_comment(pid, None, "votertester", "a comment",
                            bypass_filter=True)
    r = poster.post("/vote", data={"target_type": "comment",
                                   "target_id": str(cid),
                                   "value": "1",
                                   "csrf_token": tok,
                                   "next": "/c/lobby/post/%d" % pid})
    loc = r.headers.get("Location", "")
    check("form vote on comment -> 302", r.status_code == 302, r.status_code)
    check("form vote redirect keeps the voter on the comment anchor",
          loc == "/c/lobby/post/%d#c%d" % (pid, cid), loc)

    # --- B3: an existing fragment is never doubled ----------------------
    r = poster.post("/vote", data={"target_type": "post",
                                   "target_id": str(pid),
                                   "value": "-1",
                                   "csrf_token": tok,
                                   "next": "/c/lobby/post/%d#c%d" % (pid, cid)})
    loc = r.headers.get("Location", "")
    check("next with an existing anchor is preserved verbatim",
          loc == "/c/lobby/post/%d#c%d" % (pid, cid), loc)

    # --- B4: JSON vote path is untouched (no redirect) ------------------
    r = poster.post("/vote", json={"target_type": "post",
                                   "target_id": pid, "value": 1,
                                   "csrf_token": tok})
    check("JSON vote still returns 200 with score",
          r.status_code == 200 and r.get_json().get("ok") is True,
          r.status_code)

    print("\n%d passed, %d failed: %s" % (len(PASS), len(FAIL), FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
