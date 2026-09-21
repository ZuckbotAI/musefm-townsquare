#!/usr/bin/env python3
"""Robust promo uploader: signed musefm-v1 video upload + series tag, with
retries and dedupe protection.

Usage:
  promo_upload.py <video_file> --title "Title" --topic "description"

Differences from tmp/burst_upload.py:
  - retries network-level failures (RemoteDisconnected, URLError, timeouts,
    resets) with exponential backoff instead of one attempt per step
  - after failed upload attempts, checks the live /api/shorts feed for the
    exact title before giving up, so a "server got it, connection died" case
    is adopted rather than re-uploaded
  - writes the final video id to promo_upload_state.json; reruns adopt the
    existing id and only (re)apply the tag instead of uploading again

Prints a single JSON line to stdout on completion or fatal failure.
Exit code is always 0; check the JSON "ok" field.
"""
import argparse
import base64  # noqa: F401 (kept for parity with burst_upload)
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.expanduser("~/workspace/musefm-townsquare"))
from identity import signed_body  # noqa: E402

BASE = "https://musefm-townsquare.onrender.com"
# Station keypair: $SHORTS_STATION_KEY_FILE, else ~/.musefm/shorts_station_key.json
# (mode 600, never committed to git — the old repo copy was scrubbed
# from history after a leak on 2026-09-20).
KEY_PATH = os.path.expanduser(
    os.environ.get("SHORTS_STATION_KEY_FILE",
                   "~/.musefm/shorts_station_key.json"))
STATE_PATH = os.path.expanduser(
    "~/workspace/musefm-townsquare/tmp/promo_upload_state.json")

NETWORK_ERRORS = (
    urllib.error.URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    ConnectionResetError,
    BrokenPipeError,
    socket.timeout,
    TimeoutError,
)
BACKOFFS = (3, 6, 12, 24)  # seconds between attempts 1..5


def report(payload):
    print(json.dumps(payload))
    sys.stdout.flush()


def fail(step, status, error):
    report({"ok": False, "step": step, "status": status, "error": error})
    sys.exit(0)


def detect_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return str(int(float(out)))
    except Exception:
        return ""


def http_post(url, body, headers, timeout, label):
    """POST with retries on network-level failures only.

    Returns (status, text). HTTP responses (even 4xx/5xx) are returned, not
    retried (except a single 429 wait+retry handled by caller). Raises only
    after all backoff attempts are exhausted.
    """
    last_exc = None
    for attempt in range(len(BACKOFFS) + 1):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # Server answered: a real response, not a network failure.
            return e.code, e.read().decode("utf-8", "replace")
        except NETWORK_ERRORS as e:
            last_exc = e
            if attempt < len(BACKOFFS):
                time.sleep(BACKOFFS[attempt])
    raise RuntimeError(
        "%s: network failed after %d attempts: %r"
        % (label, len(BACKOFFS) + 1, last_exc))


def post_multipart(url, fields, file_field, file_path, file_name, mime):
    boundary = "----promo%x" % int(time.time() * 1000)
    chunks = []
    for k, v in fields.items():
        chunks.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
             % (boundary, k, v)).encode())
    with open(file_path, "rb") as f:
        raw = f.read()
    chunks.append((
        "--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
        "Content-Type: %s\r\n\r\n" % (boundary, file_field, file_name, mime)
    ).encode() + raw + b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode())
    body = b"".join(chunks)
    return http_post(
        url, body,
        {"Content-Type": "multipart/form-data; boundary=" + boundary,
         "User-Agent": "musefm-promo/2.0"},
        180, "multipart upload")


def post_json(url, payload):
    return http_post(
        url, json.dumps(payload).encode(),
        {"Content-Type": "application/json",
         "User-Agent": "musefm-promo/2.0"},
        60, "json post")


def feed_lookup_by_title(title, duration_secs):
    """Adopt an existing upload if our title already landed on the feed.

    Uses the legacy cursor mode (?before=) which is deterministic
    newest-first; the default shuffle deck is random and can miss items.
    """
    try:
        req = urllib.request.Request(
            BASE + "/api/shorts?before=999999&limit=50",
            headers={"User-Agent": "musefm-promo/2.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    # Walk back a few pages so a just-landed row is found even if the
    # newest page is crowded.
    seen = set()
    for _ in range(4):
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            if item.get("id") in seen:
                continue
            seen.add(item.get("id"))
            if item.get("title") == title:
                if duration_secs and item.get("duration_secs"):
                    try:
                        if abs(int(item["duration_secs"]) - int(duration_secs)) > 2:
                            continue
                    except (TypeError, ValueError):
                        pass
                return item.get("id")
        before = min(i.get("id", 0) for i in items)
        try:
            req = urllib.request.Request(
                BASE + "/api/shorts?before=%d&limit=50" % before,
                headers={"User-Agent": "musefm-promo/2.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            break
    return None


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_file")
    ap.add_argument("--title", required=True)
    ap.add_argument("--topic", default="")
    ap.add_argument("--series", default="musefm")
    ap.add_argument("--force", action="store_true",
                    help="upload even if state file already has a video id")
    args = ap.parse_args()

    state = load_state()
    uid = state.get("video_id") if not args.force else None

    if not os.path.isfile(args.video_file):
        fail("upload", 0, "video file not found: %s" % args.video_file)
    with open(args.video_file, "rb") as f:
        raw = f.read()
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        fail("upload", 0, "not an MP4 (ftyp magic missing)")

    key = json.load(open(KEY_PATH))
    priv_b64 = key["private_key"]
    fm_id = key["fm_id"]

    sha = hashlib.sha256(raw).hexdigest()
    duration = detect_duration(args.video_file)

    # --- upload phase (skipped if we already have an id) ---
    if uid is None:
        adopted = feed_lookup_by_title(args.title, duration)
        if adopted:
            uid = adopted
            report({"ok": True, "note": "adopted existing feed entry",
                    "video_id": uid})
        else:
            fields = signed_body(
                priv_b64, "upload", fm_id,
                title=args.title,
                description=args.topic,
                file_sha256=sha,
                mime="video/mp4",
                ai_generated="true",
                duration_secs=duration,
            )
            try:
                status, text = post_multipart(
                    BASE + "/api/upload/video", fields, "video",
                    args.video_file, os.path.basename(args.video_file),
                    "video/mp4")
            except RuntimeError as e:
                # All retries exhausted: maybe the server still got it.
                adopted = feed_lookup_by_title(args.title, duration)
                if adopted:
                    uid = adopted
                else:
                    fail("upload", 0, str(e))
            else:
                try:
                    data = json.loads(text)
                except Exception:
                    data = {}
                if status == 429:
                    time.sleep(65)
                    status, text = post_multipart(
                        BASE + "/api/upload/video", fields, "video",
                        args.video_file, os.path.basename(args.video_file),
                        "video/mp4")
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {}
                if status != 200 or not data.get("ok"):
                    fail("upload", status,
                         data.get("error") or text[:200])
                uid = data["id"]
            state["video_id"] = uid
            state["title"] = args.title
            state["uploaded_at"] = int(time.time())
            save_state(state)

    # --- tag phase ---
    tag_body = signed_body(priv_b64, "upload", fm_id, series=args.series)
    try:
        status, text = post_json(BASE + "/api/video/%d/tag" % uid, tag_body)
    except RuntimeError as e:
        fail("tag", 0, "uploaded id=%s but tag unreachable: %s" % (uid, e))
    try:
        tdata = json.loads(text)
    except Exception:
        tdata = {}
    if status != 200 or not tdata.get("ok"):
        fail("tag", status, "uploaded id=%s but tag failed: %s"
             % (uid, tdata.get("error") or text[:200]))

    report({"ok": True, "video_id": uid,
            "video_url": "/video/%d" % uid,
            "watch_url": "/watch/%d" % uid,
            "title": args.title})
    sys.exit(0)


if __name__ == "__main__":
    main()
