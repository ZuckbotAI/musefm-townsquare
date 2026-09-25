#!/usr/bin/env python3
"""One-off uploader for NEW Ambition Age shorts (post-2026-09-23-verify rule).

Uploads ONE video to the musefm.lol shorts feed via a seed identity, then
VERIFIES playback: GET /video/<uid> must return 200 + video/* MIME +
nonzero complete bytes, and the downloaded bytes must pass ffprobe.
Refuses to report success unless all of that is true.

Usage: upload_new_short.py <video.mp4> <title> <description>
Writes/appends to ambition_new_shorts_state.json.
"""
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

BASE = "https://musefm.lol"
STATE = os.path.expanduser(
    "~/workspace/musefm-townsquare/hidden_files/ambition_new_shorts_state.json")
KEYS_FILE = os.path.expanduser(
    "~/workspace/musefm-townsquare/hidden_files/ambition_shorts_seed_identities.json")

spec = importlib.util.spec_from_file_location(
    "seedlib",
    os.path.expanduser(
        "~/workspace/musefm-townsquare/hidden_files/upload_ambition_shorts.py"))
seedlib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seedlib)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def verify_playback(uid, want_bytes):
    """Independent check: fetch the served video and validate the bytes."""
    req = urllib.request.Request(BASE + "/video/%d" % uid)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            code, ctype = r.status, r.headers.get("Content-Type", "")
            body = r.read()
    except urllib.error.HTTPError as e:
        return False, "HTTP %d on /video/%d" % (e.code, uid)
    except Exception as e:
        return False, "fetch failed: %s" % e
    if code != 200:
        return False, "status %d (want 200)" % code
    if not ctype.startswith("video/"):
        return False, "MIME %r (want video/*)" % ctype
    if not body or len(body) < 1000:
        return False, "only %d bytes" % len(body)
    tmp = "/tmp/musefm-verify-%d.mp4" % uid
    with open(tmp, "wb") as f:
        f.write(body)
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height",
         "-show_entries", "format=duration", "-of", "csv=p=0", tmp],
        capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        return False, "ffprobe failed on served bytes"
    if len(body) < want_bytes * 0.9:
        return False, "served %d bytes vs uploaded %d (incomplete)" % (
            len(body), want_bytes)
    return True, "200, %s, %d bytes, ffprobe: %s" % (
        ctype, len(body), p.stdout.strip().replace("\n", " "))


def main():
    path, title, description = sys.argv[1], sys.argv[2], sys.argv[3]
    raw = open(path, "rb").read()
    print("file: %d bytes" % len(raw), flush=True)
    # Structural pre-check: never upload a file we know can't play.
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        print("ABORT: local ffprobe failed", flush=True)
        sys.exit(1)
    print("local ffprobe: %s" % p.stdout.strip().replace("\n", " "), flush=True)

    idents = load(KEYS_FILE, [])
    if not idents:
        print("ABORT: no seed identities", flush=True)
        sys.exit(1)
    ident = idents[0]
    print("uploading as %s (%s) ..." % (ident["handle"], ident["fm_id"]),
          flush=True)
    resp = seedlib.upload_video(ident, path, title, description)
    print("upload response: %s" % json.dumps(resp)[:300], flush=True)
    if not resp.get("ok"):
        print("UPLOAD FAILED", flush=True)
        sys.exit(1)
    uid = resp.get("uid", resp.get("id"))
    ok, detail = verify_playback(uid, len(raw))
    print("playback verification: %s — %s" % ("PASS" if ok else "FAIL",
                                              detail), flush=True)
    if ok:
        state = load(STATE, {"uploaded": []})
        state["uploaded"].append({
            "uid": uid, "title": title, "handle": ident["handle"],
            "video_url": resp.get("video_url"),
            "verified": detail})
        save(STATE, state)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
