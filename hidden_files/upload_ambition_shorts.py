#!/usr/bin/env python3
"""Seed Ambition Age shorts onto the musefm.lol shorts feed.

Dogfooding run at Anthony's explicit direction (2026-09-23 ~02:30 CDT):
upload all 15 Ambition Age shorts to the musefm.lol shorts feed, posted
across several newly-registered seed identities.

Honesty notes (logged, not hidden):
- The videos are REAL Ambition Age content, made by Zuckbot.
- Descriptions attribute the Ambition Age YouTube channel with links.
- ai_generated=true is truthful (these are AI-generated videos) and puts
  them live immediately instead of the mod queue.
- The seed identities are new accounts created for this seeding run; they
  do not impersonate any real muse or person.

Rate limits: per-IP 10 video uploads/hour, per-identity 20/hour.
This script uploads at most MAX_UPLOADS_PER_RUN then stops; re-run later
(it resumes from STATE_FILE).
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE = "https://musefm.lol"
STATE_FILE = os.path.expanduser(
    "~/workspace/musefm-townsquare/hidden_files/ambition_shorts_seed_state.json")
KEYS_FILE = os.path.expanduser(
    "~/workspace/musefm-townsquare/hidden_files/ambition_shorts_seed_identities.json")
MAX_BYTES = 32 * 1024 * 1024
SAFE_BYTES = 31 * 1024 * 1024
MAX_UPLOADS_PER_RUN = 9  # s01 already landed this hour (id 371); 1+9=10/hr IP cap

SHORTS_DIR = os.path.expanduser("~/workspace/ambition-age-shorts")
CHAN_URL = "https://www.youtube.com/@AmbitionAge"

# (relative dir, youtube url or None, title override or None)
SHORTS = [
    ("batch1/s01-spot-the-ai-lie", "https://www.youtube.com/shorts/kgrQpXdBRO4", None),
    ("batch1/s02-ai-force", "https://www.youtube.com/shorts/Z6suesQZT-s", None),
    ("batch1/s03-openai-robotics", "https://www.youtube.com/shorts/ov3BNEA384E", None),
    ("batch1/s04-albatross", "https://www.youtube.com/shorts/qCmrxXXVvIk", None),
    ("batch1/s05-ubi-mythbust", "https://www.youtube.com/shorts/Ch01yVC_Axk", None),
    ("batches/2026-09-22/a-robot-assembly-line", "https://www.youtube.com/shorts/tZwQx2rnIGk", None),
    ("batches/2026-09-22/b-kill-switch", "https://www.youtube.com/shorts/jbC8NAl3Ewc", None),
    ("batches/2026-09-22/c-16k-humanoid", "https://www.youtube.com/shorts/l4c0xg_V9us", None),
    ("batches/2026-09-22/d-swift-patient-zero", "https://www.youtube.com/shorts/rAlU5XlkG3g", None),
    ("batches/2026-09-22/e-obamacare-purge", "https://www.youtube.com/shorts/JzxD2OBh5vo", None),
    ("batches/2026-09-22/f-riddle-quiz", "https://www.youtube.com/shorts/6O9G4UAPsIY", None),
    ("batches/2026-09-22/g-gpt6-sol-luna", None, "AI Just Got 50% Cheaper Overnight"),
    ("batches/2026-09-22/h-real-madrid-psg", None, "Real Madrid Just Struck in the Champions League"),
    ("batches/2026-09-22/i-history-brain-busters", None, "3 History Facts That Will Break Your Brain"),
    ("batches/2026-09-23/j-daily-news-2026-09-23", None,
     "Ambition Age Daily: Moon crater, cancer-drug milestone, finger-walking robots"),
]

IDENTITIES = [
    # NOTE 2026-09-23: PixelPilot and NeonNarrator were registered but their
    # keypairs were lost to dropped connections before the fm_id came back.
    # They sit empty; do not reuse those handles.
    ("ClipCrate", "Saving the shorts worth rewatching."),
    ("VibeVault", "Future-tech and fun facts, daily."),
    ("ShortStack", "Short videos, tall curiosity."),
    ("ReelRelay", "Passing along the clips worth your minute."),
]


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class UncertainUpload(Exception):
    pass


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def post_json(path, payload, _retried=False):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # API errors are JSON with 4xx status — return the body, never retry.
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error": "HTTP %d" % e.code}
    except Exception as e:
        if _retried:
            raise
        print("  post_json %s net-error (%s) — retrying once" % (path, e),
              flush=True)
        time.sleep(3)
        return post_json(path, payload, _retried=True)


def handle_taken(handle):
    """Probe whether a handle is already registered (dummy key)."""
    try:
        resp = post_json("/api/identity/register", {
            "handle": handle,
            "public_key": b64u(b"\x00" * 32),
            "bio": "probe",
        })
    except Exception:
        return False
    err = (resp.get("error") or "")
    return "handle taken" in err


def render(v):
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


def sign_fields(fm_id, priv_b64, action, fields):
    priv = Ed25519PrivateKey.from_private_bytes(b64d(priv_b64))
    ts, nonce = str(int(time.time() * 1000)), b64u(os.urandom(16))
    merged = {"action": action, **fields}
    lines = ["musefm-v1", action, ts, nonce, fm_id]
    for k in sorted(merged):
        if k in ("signature", "timestamp", "nonce", "fm_id"):
            continue
        v = render(merged[k])
        lines.append("%s:%d:%s" % (k, len(v.encode("utf-8")), v))
    sig = b64u(priv.sign("\n".join(lines).encode("utf-8")))
    return {"action": action, "fm_id": fm_id, "timestamp": ts,
            "nonce": nonce, "signature": sig, **fields}


def register_identity(handle, bio):
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    priv_b64 = b64u(priv.private_bytes_raw())
    pub_b64 = b64u(pub.public_bytes_raw())
    try:
        resp = post_json("/api/identity/register", {
            "handle": handle,
            "public_key": pub_b64,
            "bio": bio,
        })
    except Exception as e:
        # Ambiguous: the server may have registered us before the
        # connection dropped (and the keypair would then be useless).
        # Probe to disambiguate; if taken, abandon the handle.
        print("  registration of %s errored (%s) — probing" % (handle, e),
              flush=True)
        if handle_taken(handle):
            print("  %s is taken (key lost) — skipping handle" % handle,
                  flush=True)
            return None
        resp = post_json("/api/identity/register", {
            "handle": handle,
            "public_key": pub_b64,
            "bio": bio,
        })
    if not resp.get("ok"):
        if "handle taken" in (resp.get("error") or ""):
            print("  %s already taken — skipping" % handle, flush=True)
            return None
        raise RuntimeError("register failed for %s: %s" % (handle, resp))
    return {"handle": handle, "fm_id": resp["fm_id"],
            "private_key": priv_b64, "public_key": pub_b64}


def ensure_identities():
    ids = load_json(KEYS_FILE, [])
    have = {i["handle"] for i in ids}
    for handle, bio in IDENTITIES:
        if handle in have:
            continue
        print("registering identity %s ..." % handle, flush=True)
        ident = register_identity(handle, bio)
        if ident is None:
            continue
        ids.append(ident)
        save_json(KEYS_FILE, ids)
        print("  -> %s" % ids[-1]["fm_id"], flush=True)
    if not ids:
        raise RuntimeError("no usable identities")
    return ids


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


def shrink_to_fit(src, workdir):
    """Re-encode until under SAFE_BYTES. Returns path to uploadable file."""
    if os.path.getsize(src) <= SAFE_BYTES:
        return src
    base = os.path.splitext(os.path.basename(src))[0]
    for crf in (27, 30, 33):
        dst = os.path.join(workdir, "%s-crf%d.mp4" % (base, crf))
        if not os.path.exists(dst):
            r = subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", src,
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-movflags", "+faststart", dst])
            if r.returncode != 0:
                raise RuntimeError("ffmpeg failed on %s" % src)
        sz = os.path.getsize(dst)
        print("  re-encoded crf %d -> %d bytes" % (crf, sz), flush=True)
        if sz <= SAFE_BYTES:
            return dst
    raise RuntimeError("could not shrink %s under limit" % src)


def upload_video(ident, path, title, description):
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) > MAX_BYTES:
        raise RuntimeError("file still over 32MB: %s" % path)
    sha = hashlib.sha256(raw).hexdigest()
    dur = int(probe_duration(path))
    fields = {
        "file_sha256": sha,
        "ai_generated": True,
        "title": title[:120],
        "description": description[:500],
        "duration_secs": str(dur),
    }
    form = sign_fields(ident["fm_id"], ident["private_key"], "upload", fields)
    boundary = "----MuseSeed" + b64u(os.urandom(12))
    body = b""
    for k, v in form.items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, k, v)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"video\"; "
             "filename=\"%s\"\r\nContent-Type: video/mp4\r\n\r\n"
             % (boundary, os.path.basename(path))).encode()
    body += raw + b"\r\n--%s--\r\n" % boundary.encode()
    req = urllib.request.Request(
        BASE + "/api/upload/video", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body_txt = e.read()[:300]
        except Exception:
            body_txt = b"<unreadable body>"
        raise RuntimeError("upload HTTP %d: %s" % (e.code, body_txt))
    except Exception as e:
        # Network dropped mid-upload: the video may or may not have
        # landed. Do NOT blindly retry (would duplicate); flag uncertain.
        raise UncertainUpload(str(e))


def clean_title(d, override):
    if override:
        return override
    t = open(os.path.join(d, "title.txt")).read().strip()
    return t[:-len(" #shorts")].strip() if t.endswith("#shorts") else t


def main():
    log_path = os.path.expanduser(
        "~/workspace/musefm-townsquare/hidden_files/ambition_shorts_seed.log")
    logf = open(log_path, "a", buffering=1)

    def log(*a, **k):
        msg = " ".join(str(x) for x in a)
        print(msg, flush=True)
        logf.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))

    state = load_json(STATE_FILE, {"done": {}, "run_uploads": 0})
    done = state.get("done", {})
    idents = ensure_identities()
    workdir = "/tmp/ambition-shorts-shrink"
    os.makedirs(workdir, exist_ok=True)
    uploaded_this_run = 0
    idx = 0
    for rel, yt_url, title_override in SHORTS:
        if rel in done:
            continue
        if uploaded_this_run >= MAX_UPLOADS_PER_RUN:
            break
        d = os.path.join(SHORTS_DIR, rel)
        src = os.path.join(d, "final.mp4")
        title = clean_title(d, title_override)
        if yt_url:
            desc = ("From the Ambition Age channel on YouTube. "
                    "Watch the original: %s" % yt_url)
        else:
            desc = ("From the Ambition Age channel: %s — "
                    "new shorts daily." % CHAN_URL)
        ident = idents[idx % len(idents)]
        idx += 1
        log("[%s] uploading '%s' as %s ..." % (rel, title, ident["handle"]),
              flush=True)
        try:
            path = shrink_to_fit(src, workdir)
            resp = upload_video(ident, path, title, desc)
        except UncertainUpload as e:
            log("  UNCERTAIN (net dropped): %s — flagged, moving on" % e,
                  flush=True)
            state.setdefault("uncertain", {})[rel] = {
                "title": title, "handle": ident["handle"]}
            save_json(STATE_FILE, {**state, "done": done})
            continue
        except RuntimeError as e:
            log("  FAILED: %s" % e, flush=True)
            if "429" in str(e) or "rate limit" in str(e).lower():
                log("  rate limited — stopping this run", flush=True)
                break
            continue
        except Exception as e:
            # Belt and suspenders: one bad short must never kill the run.
            log("  UNEXPECTED FAILURE (%s: %s) — skipping" % (type(e).__name__, e),
                  flush=True)
            continue
        if not resp.get("ok"):
            log("  FAILED: %s" % resp, flush=True)
            continue
        done[rel] = {"uid": resp.get("uid"),
                     "video_url": resp.get("video_url"),
                     "handle": ident["handle"]}
        uploaded_this_run += 1
        log("  OK uid=%s url=%s" % (resp.get("uid"), resp.get("video_url")),
              flush=True)
        time.sleep(2)
    state["done"] = done
    save_json(STATE_FILE, state)
    remaining = [s[0] for s in SHORTS if s[0] not in done]
    log("uploaded this run: %d, remaining: %d" % (uploaded_this_run,
                                                   len(remaining)))
    log("REMAINING=" + ",".join(remaining))


if __name__ == "__main__":
    main()
