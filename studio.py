#!/usr/bin/env python3
"""Studio: in-world short generation for the Maker's Row studio building.

This module owns the *generation* side of shorts. The existing shorts
endpoints (/api/upload/video, /api/shorts) are for *uploads* of finished
MP4s -- this module RENDERS a real MP4 from submitted text via ffmpeg:

    POST /api/studio/generate   session-auth + CSRF, like /api/drift/adopt
    GET  /api/studio/status/<job_id>   poll until status == "ready"
    GET  /studio/<job_id>.mp4   the finished, playable MP4

No mocks: a job only flips to "ready" after ffmpeg has written real bytes
to disk. Failures flip to "failed" with the ffmpeg stderr tail as error.

Rendering recipe (borrowed from the proven tmp/gen_shorts_batch.py batch
pipeline): animated gradient background + timed burned-in text + grain,
1080x1920 H.264, quiet generated sine-pad bed, +faststart. ffmpeg must be
on PATH (it is on dev and Render).
"""

import os
import re
import secrets
import subprocess
import sys
import threading
import time

W, H = 1080, 1920
FPS = 30
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

FONT_BLACK = "/usr/share/fonts/truetype/noto/NotoSans-Black.ttf"
FONT_XBOLD = "/usr/share/fonts/truetype/noto/NotoSans-ExtraBold.ttf"

# Style palettes: gradient corner colors + accent color for the end card.
STYLES = {
    "night": {"c": ("0x0a0f2e", "0x1a1440", "0x0d1b3d", "0x241040"),
              "accent": "0x9fd8ff", "speed": 0.07},
    "arena": {"c": ("0x2e0a0a", "0x401414", "0x1a0d0d", "0x3d1010"),
              "accent": "0xffd166", "speed": 0.09},
    "teal":  {"c": ("0x0a2e28", "0x0d3d33", "0x0a1f2e", "0x144040"),
              "accent": "0xa8e6cf", "speed": 0.07},
    "slate": {"c": ("0x0a1a2e", "0x14243d", "0x0d1426", "0x1c2f47"),
              "accent": "0x9fd8ff", "speed": 0.07},
    "void":  {"c": ("0x050508", "0x0d0d18", "0x080810", "0x141428"),
              "accent": "0xcfd8ff", "speed": 0.05},
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS studio_jobs (
    job_id     TEXT PRIMARY KEY,
    fm_id      TEXT NOT NULL,
    handle     TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    style      TEXT NOT NULL DEFAULT 'night',
    status     TEXT NOT NULL DEFAULT 'pending',
    error      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    finished_at REAL
);
"""


def ensure_studio_schema(db):
    db._exec(SCHEMA_SQL)


def _dt_escape(s):
    # Unquoted option-value form (no '...' quoting): escape the filter
    # separators and drawtext-expansion chars. Quoted values break when
    # text contains an escaped quote AND a later quoted option (like
    # enable='between(t,0.0,3.8)') -- the parser mis-splits the chain.
    return (s.replace("\\", "\\\\").replace("'", "\\'")
             .replace(":", "\\:").replace(",", "\\,").replace(";", "\\;")
             .replace("%", "\\%").replace("[", "\\[").replace("]", "\\]")
             .replace("\n", " "))


def _font_ok():
    return os.path.isfile(FONT_BLACK) and os.path.isfile(FONT_XBOLD)


def chunk_text(text, max_chars=40):
    """Split text into display chunks of <= max_chars, word-safe."""
    words = text.split()
    chunks, cur = [], ""
    for w in words:
        probe = (cur + " " + w).strip()
        if len(probe) <= max_chars:
            cur = probe
        else:
            if cur:
                chunks.append(cur)
            cur = w if len(w) <= max_chars else w[:max_chars]
    if cur:
        chunks.append(cur)
    return chunks or [text[:max_chars]]


def duration_for(chunks):
    """4s per chunk + 2.5s end card, clamped to 10-20s."""
    return min(20, max(10, len(chunks) * 4.0 + 2.5))


def render_short(text, style, out_path):
    """Render text as a vertical MP4 via ffmpeg. Raises RuntimeError on
    failure (with the ffmpeg stderr tail). Writes a REAL playable file."""
    if style not in STYLES:
        raise ValueError("unknown studio style: %r" % (style,))
    pal = STYLES[style]
    chunks = chunk_text(text.strip())
    chunks = chunks[:4]  # keep renders short and snappy
    dur = duration_for(chunks)
    end_t0 = len(chunks) * 4.0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    dt = []
    for i, chunk in enumerate(chunks):
        t0, t1 = i * 4.0, i * 4.0 + 3.8
        font = FONT_BLACK if i == 0 else FONT_XBOLD
        size = 120 if len(chunk) <= 16 else (96 if len(chunk) <= 26 else 78)
        dt.append(
            "drawtext=fontfile=%s:text=%s:fontsize=%d:"
            "fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:"
            "shadowcolor=black@0.65:shadowx=0:shadowy=5:"
            "enable=between\\(t\\,%s\\,%s\\)" % (
                font, _dt_escape(chunk), size, t0, t1))
    dt.append(
        "drawtext=fontfile=%s:text=muse\\ fm:fontsize=64:"
        "fontcolor=%s:x=(w-text_w)/2:y=(h-text_h)/2:"
        "shadowcolor=black@0.65:shadowx=0:shadowy=5:"
        "enable=between\\(t\\,%s\\,%s\\)" % (FONT_XBOLD, pal["accent"], end_t0, dur))

    vf = ",".join(dt + ["noise=alls=4:allf=t", "format=yuv420p"])
    c0, c1, c2, c3 = pal["c"]
    src = ("gradients=size=%dx%d:speed=%s:nb_colors=4:"
           "c0=%s:c1=%s:c2=%s:c3=%s" % (W, H, pal["speed"], c0, c1, c2, c3))
    # Quiet generated pad bed: two sine tones mixed low. Real audio,
    # generated locally -- no external assets, no fake "music".
    audio1 = "sine=frequency=174:duration=%s" % dur
    audio2 = "sine=frequency=261.63:duration=%s" % dur
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", src,
           "-f", "lavfi", "-i", audio1,
           "-f", "lavfi", "-i", audio2,
           "-t", str(dur),
           "-vf", vf,
           "-r", str(FPS),
           "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first[a0];"
                              "[a0]volume=0.15[a]",
           "-map", "0:v", "-map", "[a]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "96k",
           "-movflags", "+faststart",
           out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise RuntimeError("ffmpeg failed: %s" % (r.stderr or "unknown")[-600:])
    if not os.path.isfile(out_path) or os.path.getsize(out_path) < 1024:
        raise RuntimeError("ffmpeg produced no usable output")
    return out_path


def create_job(db, fm_id, handle, text, style, out_dir):
    job_id = secrets.token_urlsafe(16)
    db._exec(
        "INSERT INTO studio_jobs (job_id, fm_id, handle, text, style, "
        "status, created_at) VALUES (?,?,?,?,?,'pending',?)",
        (job_id, fm_id, handle, text, style, time.time()))
    return job_id


def get_job(db, job_id):
    if not JOB_ID_RE.match(job_id or ""):
        return None
    rows = db._q("SELECT * FROM studio_jobs WHERE job_id=?", (job_id,))
    return dict(rows[0]) if rows else None


def artifact_path(out_dir, job_id):
    return os.path.join(out_dir, job_id + ".mp4")


def _job_worker(db, out_dir, job_id):
    """Background render. db is the shared Database object -- its .db
    property is threading.local, so this thread gets its own connection."""
    try:
        db._exec("UPDATE studio_jobs SET status='running' WHERE job_id=?",
                 (job_id,))
        rows = db._q("SELECT text, style FROM studio_jobs WHERE job_id=?",
                     (job_id,))
        if not rows:
            return
        row = dict(rows[0])
        render_short(row["text"], row["style"],
                     artifact_path(out_dir, job_id))
        db._exec("UPDATE studio_jobs SET status='ready', finished_at=? "
                 "WHERE job_id=?", (time.time(), job_id))
    except Exception as e:  # never kill the process on a bad render
        sys.stderr.write("[studio] job %s failed: %r\n" % (job_id, e))
        try:
            db._exec("UPDATE studio_jobs SET status='failed', error=?, "
                     "finished_at=? WHERE job_id=?",
                     (str(e)[:600], time.time(), job_id))
        except Exception:
            pass


def launch_job(db, out_dir, job_id):
    t = threading.Thread(target=_job_worker, args=(db, out_dir, job_id),
                         daemon=True, name="studio-render-%s" % job_id[:8])
    t.start()
    return t


def studio_out_dir(data_dir):
    d = os.path.join(data_dir, "studio")
    os.makedirs(d, exist_ok=True)
    return d
