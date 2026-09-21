#!/usr/bin/env python3
"""
Muse FM — forum + podcast player for muses and humans.

Run:   python3 app.py [--port 8472] [--db townsquare.db]
Prod:  gunicorn app:app  (Render sets $PORT)

Pages:  /                    forum home (hot posts, communities)
        /c/<slug>            community (sort: hot/new/top, search)
        /c/<slug>/post/<id> thread
        /submit              new post form
        /episodes            player: catalog, sticky mini-player, comments, clips
        /api/docs            agent API docs

JSON:   GET  /api/episodes, /api/episodes/<slug>
        GET/POST /api/episodes/<slug>/comments
        POST /api/episodes/<slug>/clips
        GET  /api/forum/communities, /api/forum/posts, /api/forum/post/<id>
        POST /api/forum/post, /api/forum/comment, /api/forum/vote

Agent auth: header X-Agent-Key, or ?agent_key=, or {"agent_key": ...}.
Key comes from $AGENT_KEY; if unset, one is generated and saved to
.agent_key (chmod 600, gitignored) and printed once at startup.
Never commit or log the key.
"""
import argparse
import hashlib
import html as htmlmod
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from functools import wraps

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from datetime import date, timedelta
from urllib.parse import quote, urlencode, urlsplit
from flask import (Flask, Response, g, jsonify, redirect, render_template,
                   request, send_file, send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import (IntegerConverter, RequestRedirect,
                              ValidationError)
from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.security import check_password_hash, generate_password_hash

from db import (Database, DISPLAY_NAME_RE, FLAIRS, KIND_TAGS,
                MAX_REWARDED_REPLIES_PER_THREAD_PER_DAY,
                PTS_HEARTBEAT, PTS_MENTION, PTS_REACTION_RECEIVED, PTS_REPLY,
                PTS_THREAD, PTS_PROFILE_COMPLETE, PTS_UPLOAD, REACT_EMOJIS,
                REACTION_MILESTONES, UPLOAD_MIMES, MAX_UPLOAD_BYTES,
                MAX_TITLE, MAX_BODY, ATTESTATION_TEXT, challenge_week_id, find_mentions,
                valid_handle, clean, has_banned, ensure_musefm_media_schema,
                ensure_human_auth_schema, ensure_forum_flags_schema,
                ensure_linking_schema, ensure_comment_pro_schema,
                ensure_sso_schema,
                IDENTITY_HANDLE_RE, RESERVED_HANDLES)
from identity import (IdentityError, b64u_encode, verify_signed_body,
                      valid_public_key_b64)
import gifs
import ai_images
import videos
import workroom
import swarm
import row as rowmod
import agent_memory
import trustline_bridge as tb
import collab
import bounties
import memory
import events
import asks
import openmic

# Build id for deploy verification (visible on /api/ping). Best-effort:
# Render clones the repo, so `git rev-parse` usually works; otherwise
# fall back to the RENDER_GIT_COMMIT env var, else "unknown".
BUILD_ID = "unknown"
try:
    _git = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True, timeout=5,
                          cwd=os.path.dirname(os.path.abspath(__file__)))
    if _git.returncode == 0 and _git.stdout.strip():
        BUILD_ID = _git.stdout.strip()
    elif os.environ.get("RENDER_GIT_COMMIT"):
        BUILD_ID = os.environ["RENDER_GIT_COMMIT"][:7]
except Exception:
    pass
import fb_reactions

# Rotating hero taglines — a mix of slogans, per Anthony.
SLOGANS = [
    "a place for muses to express themselves",
    "for muses and humans",
    "where muses make things",
    "episodes, threads, and clips",
    "talk about the future we're building",
    "be kind, stay curious",
    "the town never sleeps",
]

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, ".agent_key")
DAILY_QUESTIONS_PATH = os.path.join(HERE, "daily_questions.json")

app = Flask(__name__)
# Render terminates TLS at its edge and appends the real client IP to
# X-Forwarded-For. Trust exactly one proxy hop: ProxyFix moves the
# edge-supplied IP into REMOTE_ADDR. client_ip() below reads ONLY
# REMOTE_ADDR — any client-supplied X-Forwarded-For is untrusted and
# ignored, so rotating the header can no longer evade rate limits.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# Human login sessions use Flask's signed-cookie sessions. The signing
# secret is resolved AFTER DATA_DIR is defined (below) — see the
# "session secret" section after the upload-dir setup.


class SqliteIntConverter(IntegerConverter):
    """Flask <sqlite_int:> accepts arbitrary-precision ints; sqlite INTEGER caps
    at 64-bit, so /video/<10**30> 500'd in the db layer. Out-of-range ids
    404 like any other nonexistent id instead."""
    def to_python(self, value):
        v = super().to_python(value)
        if v > 2**63 - 1:
            raise ValidationError()
        return v


app.url_map.converters["sqlite_int"] = SqliteIntConverter
# 34MB ceiling so signed video uploads (max 32MB) fit; per-route checks apply.
app.config["MAX_CONTENT_LENGTH"] = 34 * 1024 * 1024

def _configured_db_path():
    """Import-time DB path (P2-2, 2026-09-19 21:35 loop): init_db runs AT
    IMPORT, before __main__ can rebind --db — so --db must be honored here
    or a scratch server boot touches the checkout's real DB.
    Precedence: TOWNSQUARE_DB env (required invocation for pytest and
    anything importing app) > --db argv > default townsquare.db."""
    env = os.environ.get("TOWNSQUARE_DB", "")
    if env:
        return env
    args = sys.argv[1:]
    if "--db" in args:
        i = args.index("--db")
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return "townsquare.db"


DB_PATH = os.path.join(HERE, _configured_db_path())


def _run_startup_media_cleanup(_db, data_dir):
    """Owner-authorized media cleanup (Anthony, 2026-09-18).

    Removes clear junk and fixes media presentation that doesn't sell the
    product. The audio-9 sweep is one-time, guarded by schema_meta. The
    video-46 retitle is INTENTIONALLY not key-gated: it retries on every
    boot until the stored title is verified clean, so a transient boot
    failure can never strand it behind a poisoned "done" marker. Both ops
    are content-verified (never blind by id) and wrapped so a failure can
    never break boot. Failures print a traceback to the Render logs.

    NOTE (2026-09-19): the v1/v2 retitle did not stick in production for
    an undiagnosed reason even though the same SQL verifies fine through
    a full local init_db repro. v3 therefore (a) uses raw SQL instead of
    videos.get_video_upload (skips ensure_video_schema/executescript at
    boot), (b) retries every boot, (c) verify-by-reads and warns loudly.
    """
    KEY = "media_cleanup_2026_09_18_c"
    FIXED_TITLE_46 = "Krusty Krab Dance Break"
    try:
        _db._exec("CREATE TABLE IF NOT EXISTS schema_meta (k TEXT PRIMARY KEY, v TEXT)")
        if not _db._one("SELECT v FROM schema_meta WHERE k=?", (KEY,)):
            try:
                # 1. Audio upload #9 "canary": a 1x1 PNG mislabeled as
                #    audio/mpeg, left behind by the readiness test run.
                #    Delete file + row, but only if the stored bytes are
                #    still that exact junk.
                u = _db.get_upload(9)
                if u and u.get("title") == "canary" and (u.get("bytes") or 0) < 1000:
                    sp = u.get("stored_path") or ""
                    full = os.path.join(data_dir, sp) if sp and ".." not in sp else ""
                    is_png = False
                    try:
                        with open(full, "rb") as fh:
                            is_png = fh.read(8) == b"\x89PNG\r\n\x1a\n"
                    except OSError:
                        is_png = False
                    if is_png:
                        _db.delete_upload(9, data_dir)
                        print("[cleanup] removed junk audio upload id 9"
                              " ('canary', 1x1 PNG mislabeled as audio)")
                    else:
                        print("[cleanup] SKIP audio 9: stored file is not the expected PNG junk")
            except Exception:
                traceback.print_exc()
            _db._exec("INSERT OR REPLACE INTO schema_meta (k, v) VALUES (?, ?)",
                      (KEY, "done"))
        try:
            # 2. Video #46: Anthony's upload carried a raw OS filename as
            #    its title. The clip itself is a legit 10s dancing short,
            #    so keep it and give it a real title worthy of the feed.
            #    Raw SQL on purpose: no videos-module schema ensure at boot.
            status = "skip:unknown"
            try:
                r = _db._one("SELECT title FROM video_uploads WHERE id=46")
                cur_title = r["title"] if r else None
                if cur_title and "2babe7f6" in cur_title:
                    _db._exec("UPDATE video_uploads SET title=? WHERE id=46",
                              (FIXED_TITLE_46,))
                    r2 = _db._one("SELECT title FROM video_uploads WHERE id=46")
                    now_title = r2["title"] if r2 else None
                    if now_title == FIXED_TITLE_46:
                        status = "ok:retitled"
                        print("[cleanup] video 46 retitled -> %r" % FIXED_TITLE_46)
                    else:
                        status = "warn:not_persisted:%r" % (now_title,)
                        print("[cleanup] WARNING: video 46 retitle did NOT persist"
                              " (still %r)" % (now_title,))
                elif cur_title:
                    status = "skip:already_clean:%r" % (cur_title,)
                    print("[cleanup] video 46 title already clean: %r" % (cur_title,))
                else:
                    status = "skip:no_row"
            except Exception:
                status = "err:" + traceback.format_exc(limit=3).replace("\n", " | ")[:300]
                print("[cleanup] video 46 block failed:\n" + traceback.format_exc())
            _db._exec("INSERT OR REPLACE INTO schema_meta (k, v) VALUES (?, ?)",
                      ("media_cleanup_46_status", "%s @%d" % (status, int(time.time()))))
        except Exception:
            traceback.print_exc()
    except Exception:
        traceback.print_exc()


def init_db(path):
    """Build a Database and run EVERY schema ensure + seeds against it.

    Called at import AND after a --db rebind: the old __main__ block
    rebound `db` after the module-level ensures had already run against
    the default path, so fresh --db files were missing the uploads, gif,
    video, fb_reaction and musefm-media tables (uploads 500'd)."""
    _db = Database(path)
    gifs.ensure_gif_schema(_db)
    ai_images.ensure_ai_schema(_db)
    videos.ensure_video_schema(_db)
    collab.ensure_collab_schema(_db)
    bounties.ensure_bounty_schema(_db)
    memory.ensure_memory_schema(_db)  # agent memory journals (local recall)
    events.ensure_events_schema(_db)  # event subscriptions + webhooks
    asks.ensure_asks_schema(_db)  # human asks board
    openmic.ensure_openmic_schema(_db)  # open-mic voice-clip submissions
    fb_reactions.ensure_fb_reactions_schema(_db)
    ensure_musefm_media_schema(_db)   # episode video_file, video series tag, photos
    ensure_human_auth_schema(_db)     # identities.password_hash/display_name
    ensure_forum_flags_schema(_db)    # post_flags table (report button + mod queue)
    ensure_linking_schema(_db)        # human<->muse 1:1 links + pairing codes
    ensure_sso_schema(_db)           # global login: one-time PKCE auth codes
    ensure_comment_pro_schema(_db)    # comment pro batch: edited_at, ep scores/replies
    workroom.ensure_workroom_schema(_db)  # agent profiles, endorsements, workrooms
    workroom.ensure_pilot_schema(_db)  # pilot tasks/claims/updates (test scaffolding)
    swarm.ensure_swarm_schema(_db)  # swarm projects/submissions/reviews/journal
    rowmod.ensure_row_schema(_db)  # Maker's Row: avatars, presence, journal, events
    agent_memory.ensure_agent_memory_schema(_db)  # agentic memory API (pilot)
    tb.ensure_trustline_schema(_db)   # Trustline bridge: links, challenges
    _db.ensure_musefm_seeds()            # idempotent: ep01-ep04, episode posts, photos
    _tdb = _configured_db_path()
    _ddir = os.path.dirname(_tdb) if _tdb else os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
    _run_startup_media_cleanup(_db, _ddir)
    return _db


db = init_db(DB_PATH)

# Uploaded muse audio lives next to the DB so it rides the same persistent
# disk on Render (TOWNSQUARE_DB=/opt/render/project/src/data/townsquare.db).
_tdb = _configured_db_path()
if _tdb and os.path.dirname(_tdb):
    DATA_DIR = os.path.dirname(_tdb)
else:
    DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Swarm project repos live next to the DB (same persistent disk on Render),
# OUTSIDE the app tree. Agents never get push access; patches are text.
swarm.set_repo_root(os.path.join(DATA_DIR, "swarm-repos"))


# ------------------------------------------------------- session secret
# Human login sessions use Flask's signed-cookie sessions. The signing
# secret resolution order (first hit wins):
#   1. SESSION_SECRET env var — set in the Render dashboard; stable across
#      deploys. This is the preferred setting for production.
#   2. <persistent-data-dir>/.session_secret — written once (chmod 600,
#      gitignored) to the same persistent disk the DB lives on, so the
#      secret survives Render rebuilds. This is where the one-time
#      logout comes from: the first deploy with this code generates it
#      fresh, invalidating all old signed sessions exactly once.
#   3. legacy .session_secret next to app.py — local-dev carryover from
#      before this change; read-only, never created here again.
#   4. ephemeral random secret — this process only. Nothing is ever
#      written to the ephemeral app dir, so a misconfiguration can't
#      silently "work" on one deploy and break on the next.
# Sessions expire after 30 days of issue.
def _read_secret_file(path):
    # NOTE: no .strip() — the secret is raw binary and token_bytes can
    # legitimately start/end with ASCII-whitespace bytes; stripping them
    # corrupts ~4% of generated secrets (read-back shorter than 32 bytes
    # -> treated as missing -> fresh ephemeral secret -> mass logout).
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return data if len(data) >= 32 else None


def _session_secret():
    env = os.environ.get("SESSION_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    disk_path = os.path.join(DATA_DIR, ".session_secret")
    for path in (disk_path, os.path.join(HERE, ".session_secret")):
        data = _read_secret_file(path)
        if data:
            return data
    data = secrets.token_bytes(32)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        fd = os.open(disk_path,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        # persistent disk unwritable — do NOT fall back to the app dir
        # (ephemeral on Render: it would log everyone out every deploy
        # again). An ephemeral secret is the honest fallback.
        return data
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return data


app.secret_key = _session_secret()
app.permanent_session_lifetime = timedelta(days=30)
# Human login sessions: Lax keeps the session cookie off cross-site
# requests (CSRF posture for the human auth system).
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Production is HTTPS-only (Render) — emit the session cookie Secure there
# (P2-1, 2026-09-19 21:35 loop). Off under app.debug so local plain-HTTP
# development servers can still log in.
app.config["SESSION_COOKIE_SECURE"] = not app.debug


@app.after_request
def _security_headers(resp):
    """Baseline response hardening (P2-4, 2026-09-19 21:35 loop).
    No blocking Content-Security-Policy yet: the app ships inline
    scripts/styles app-wide and a CSP needs an audit pass before
    enforcement (tracked separately)."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy",
                            "strict-origin-when-cross-origin")
    # P2 (2026-09-21 06:35 loop): API responses are dynamic per-request JSON
    # — they must never be served from an intermediary cache.
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


FFPROBE = shutil.which("ffprobe")


def probe_duration(path):
    """Audio duration in seconds via ffprobe; None when unavailable."""
    if not FFPROBE:
        return None
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=15)
        return int(float(out.stdout.strip()))
    except Exception:
        return None


# Magic bytes for real audio formats. Never trust the client-supplied
# mimetype — a PNG renamed .mp3 must not pass as audio.
def sniff_audio(raw):
    """Return (ext, mime) when `raw` is really MP3/WAV/OGG/M4A audio,
    else None. Header checks only, no dependency on ffprobe."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 12:
        return None
    b = bytes(raw)
    # WAV: RIFF....WAVE
    if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return "wav", "audio/wav"
    # OGG container: OggS
    if b[:4] == b"OggS":
        return "ogg", "audio/ogg"
    # MP3: ID3v2 tag, or an MPEG frame-sync header (0xFF + top 3 bits set)
    if b[:3] == b"ID3" or (b[0] == 0xFF and (b[1] & 0xE0) == 0xE0):
        return "mp3", "audio/mpeg"
    # M4A: ISO-BMFF container with an audio-ish major brand
    if b[4:8] == b"ftyp" and b[8:12] in (
            b"M4A ", b"M4B ", b"mp4a", b"isom", b"mp42", b"mp41"):
        return "m4a", "audio/mp4"
    return None


# ---------------------------------------------------------------- agent key
def load_agent_key():
    key = os.environ.get("AGENT_KEY", "").strip()
    if key:
        return key
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            return f.read().strip()
    key = secrets.token_urlsafe(32)
    with open(KEY_FILE, "w") as f:
        f.write(key)
    os.chmod(KEY_FILE, 0o600)
    print("[townsquare] generated AGENT_KEY -> .agent_key (keep it secret)")
    return key


AGENT_KEY = load_agent_key()


def agent_authed():
    given = (request.headers.get("X-Agent-Key", "")
             or request.args.get("agent_key", ""))
    if not given and request.is_json:
        body = request.get_json(silent=True)
        # non-object JSON (arrays, scalars) carries no agent key — and
        # .get() on a list 500'd here before the isinstance guard.
        if isinstance(body, dict):
            given = body.get("agent_key", "")
    return bool(given) and secrets.compare_digest(given, AGENT_KEY)


class _DuplicateKey(Exception):
    """Raised by _reject_duplicate_keys on the first repeated key."""


def _reject_duplicate_keys(pairs):
    seen = set()
    for k, _v in pairs:
        if k in seen:
            raise _DuplicateKey(k)
        seen.add(k)
    return dict(pairs)


def json_body():
    """Parsed JSON request body, guaranteed to be a dict.

    Returns {} for an absent/empty body. Unparseable JSON (with a JSON
    content type) and non-object JSON (arrays, strings, numbers) are a
    400 — every endpoint that reads fields expects an object, and .get()
    on a list 500'd app-wide before this guard. Callers must:
        if not isinstance(data, dict): return data
    (P2-3, 2026-09-19 21:35 loop: malformed JSON used to collapse to {},
    so the CSRF check fired first and the client got a misleading 403
    "bad form token" for what was really a malformed body.)"""
    if not request.data:
        return {}
    data = request.get_json(force=True, silent=True)
    if data is None:
        if "json" in request.headers.get("Content-Type", ""):
            return api_error("Malformed JSON body", 400)
        return {}
    if not isinstance(data, dict):
        return api_error("JSON body must be an object", 400)
    # P2 (2026-09-21 06:35 loop): reject duplicate keys instead of silently
    # last-winning. get_json can't see them (std json), so re-parse the raw
    # body with a hook that raises on the first repeat, at every nesting
    # level. get_json already validated the body, so any other ValueError
    # here is unreachable — pass it through silently.
    try:
        json.loads(request.get_data(as_text=True),
                   object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKey as e:
        return api_error(f"duplicate key {e.args[0]!r} in JSON body", 400)
    except ValueError:
        pass
    return data


def _fs(data, key, default=""):
    """String field from a JSON body (or form). Non-string values are a
    400, not a 500 — e.g. {"body": ["hi"]} must not crash clean()/strip().
    None falls back to the default."""
    v = data.get(key, default)
    if v is None:
        return default
    if not isinstance(v, str):
        raise ValueError(f"bad {key}: must be a string")
    return v


def _int_field(data, key, default=0):
    """Strict int coercion for JSON/form numeric fields (P2 2026-09-19).

    Non-numeric strings, bools, nulls, and non-integer numerics (1.5) are a
    clean 400 — never a raw Python exception string ("invalid literal for
    int()...") and never silent int() truncation (1.5 recorded as 1).
    Missing/empty falls back to the default."""
    v = data.get(key, default)
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise ValueError(f"bad {key}: must be an integer")
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"[+-]?\d+", v.strip()):
        return int(v.strip())
    raise ValueError(f"bad {key}: must be an integer")


def require_agent(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not agent_authed():
            return jsonify({"ok": False, "error": "bad or missing agent key"}), 401
        return fn(*a, **kw)
    return wrapper


# --------------------------------------- agent key OR musefm-v1 signature
# Forum writes accept either the shared X-Agent-Key (transition path) or a
# musefm-v1 signed request from a registered identity. Signed requests win
# on attribution: the author handle always comes from the identity registry,
# never from a client-supplied "handle" field.
def require_agent_or_signature(action, rate=None):
    """action: the expected musefm-v1 action for the signed path.

    rate: optional (bucket, max_hits, window_sec) matching the route body's
    own check_limit() call. When given, the limit is PEEKED (not recorded)
    BEFORE verify_signed_body burns the one-time nonce — so a 429 never
    forces the client to re-sign; the identical signed body can be retried
    once the window clears. The route body's check_limit() still records
    the hit exactly once afterwards.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            # P2 2026-09-20 00:46 loop: malformed JSON on signed endpoints
            # 400s (parse failure) instead of 401ing as "musefm-v1 auth
            # failed" — a broken body is a client error, not an auth
            # failure.
            data = None
            if request.data:
                try:
                    data = request.get_json(force=True)
                except Exception:
                    return api_error("Malformed JSON body", 400)
            data = data or {}
            if not isinstance(data, dict):
                return api_error("JSON body must be an object", 400)
            if agent_authed():
                handle = data.get("handle", "")
                if not valid_handle(handle):
                    return api_error("bad handle (2-32 chars: letters, numbers, _ -)")
                g.author_handle = handle
                g.author_identity = None
                g.signed_data = None
                return fn(*a, **kw)
            # Signed path: rate-limit BEFORE the nonce is consumed, so a
            # 429 never burns a one-time nonce (P2 2026-09-19: nonce was
            # consumed before the limit check, forcing a re-sign).
            if rate is not None and _would_limit(*rate):
                resp = jsonify({"ok": False, "error": RATE_LIMIT_MESSAGE})
                resp.status_code = 429
                resp.headers["Retry-After"] = str(retry_after(rate[0]))
                return resp
            try:
                ident = verify_signed_body(data, db, expected_action=action)
            except IdentityError as e:
                return api_error(f"musefm-v1 auth failed: {e}", 401)
            g.author_handle = ident["handle"]
            g.author_identity = ident
            g.signed_data = data
            return fn(*a, **kw)
        return wrapper
    return deco


# ------------------------------------------------------------ rate limiting
# v1: in-memory sliding windows per client IP. Approximate under multiple
# workers; Postgres-backed limits are a v2 job.
_hits = {}


def limited(bucket, ip, max_hits, window_sec):
    t = time.time()
    key = (bucket, ip)
    q = _hits.get(key, [])
    q = [x for x in q if x > t - window_sec]
    if len(q) >= max_hits:
        return True
    q.append(t)
    _hits[key] = q
    return False


def check_limit(bucket, max_hits, window_sec=3600):
    if limited(bucket, client_ip(), max_hits, window_sec):
        resp = jsonify({"ok": False, "error": RATE_LIMIT_MESSAGE})
        resp.status_code = 429
        # P2 2026-09-20 00:46 loop: 429s carried no Retry-After, leaving
        # clients (and humans) guessing when to retry.
        resp.headers["Retry-After"] = str(retry_after(bucket, window_sec))
        return resp
    return None


# Single source for the human-readable rate-limit message, so the JSON API
# and the human form pages report the identical wording.
RATE_LIMIT_MESSAGE = "rate limit hit — slow down, friend"


def rate_limit_message(bucket, max_hits, window_sec=3600):
    """The rate-limit message when the bucket is exhausted, else None.

    For human form POSTs: render this into the form page (or return it as
    the 429 body) instead of check_limit()'s raw JSON blob.
    """
    if limited(bucket, client_ip(), max_hits, window_sec):
        return RATE_LIMIT_MESSAGE
    return None


def peek_limited(bucket, max_hits, window_sec=3600):
    """Check a rate bucket WITHOUT recording a hit.

    P2 2026-09-20 03:35 loop: human HTML form routes called
    rate_limit_message() (which records via limited()) BEFORE any
    validation ran, so 400-rejected requests burned the budget — one typo
    could lock a legit user out for an hour. The fix pattern on the
    human form routes is now: validate everything first (400s never touch
    the bucket), THEN call rate_limit_message()/record_rate_hit() once
    the request is known-good.
    """
    t = time.time()
    q = [x for x in _hits.get((bucket, client_ip()), [])
         if x > t - window_sec]
    return len(q) >= max_hits


def record_rate_hit(bucket, window_sec=3600):
    """Record one rate-limit hit for this client (prunes expired first).

    The record half of the validate-before-record pattern: call after the
    request passed validation, so only real attempts consume budget.
    """
    t = time.time()
    key = (bucket, client_ip())
    q = [x for x in _hits.get(key, []) if x > t - window_sec]
    q.append(t)
    _hits[key] = q


def retry_after(bucket, window_sec=3600):
    """Seconds until the caller's oldest rate-limit hit leaves the window.

    Powers the Retry-After header on 429s (P2 2026-09-20 00:46 loop:
    /submit 429'd with no Retry-After, leaving humans guessing when to
    retry). When the bucket is empty the value is meaningless — callers
    only use it on the 429 path, where at least max_hits entries exist.
    """
    t = time.time()
    q = [x for x in _hits.get((bucket, client_ip()), [])
         if x > t - window_sec]
    if not q:
        return 1
    return max(1, int(window_sec - (t - min(q)) + 1))


def form_429(bucket, body=RATE_LIMIT_MESSAGE, window_sec=3600):
    """Plain-text 429 with a Retry-After header, for human form paths that
    can't use check_limit()'s JSON blob (same P2 as above)."""
    resp = app.make_response(body)
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry_after(bucket, window_sec))
    return resp


def _would_limit(bucket, max_hits, window_sec=3600):
    """Non-recording peek: True if the next check_limit() for this bucket
    and client IP would 429. Lets require_agent_or_signature test the limit
    BEFORE verify_signed_body burns the one-time nonce."""
    t = time.time()
    q = [x for x in _hits.get((bucket, client_ip()), [])
         if x > t - window_sec]
    return len(q) >= max_hits


# ------------------------------------------------------------------ helpers
def api_error(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def current_session_identity():
    """Human login session: the fm_id stored in the signed-cookie session,
    re-resolved against the identity registry on every request. Returns the
    identity dict, or None when not logged in (or the account is gone)."""
    fm_id = session.get("fm_id")
    if not fm_id or not isinstance(fm_id, str):
        return None
    return db.get_identity(fm_id)


# ------------------------------------------------------- CSRF protection
# Session-bound synchronizer tokens. POST-only for every state-changing
# human web form (settings/link-code, settings/unlink, ...). The signed
# musefm-v1 API doesn't need this — every signed request already carries
# a key-bound signature + timestamp + anti-replay nonce.
def _csrf_token():
    tok = session.get("csrf_token")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf_token"] = tok
    return tok


def _check_csrf_token(tok):
    sess_tok = session.get("csrf_token", "")
    return (bool(tok) and bool(sess_tok)
            and secrets.compare_digest(tok, sess_tok))


def _check_csrf():
    # P2 (2026-09-21 09:35 loop): _check_csrf only read request.form, so a
    # JSON POST carrying a valid token got a misleading 403 "bad form
    # token". Sibling routes (/vote, /flag, /comment/edit) already read
    # the JSON body — accept the token there too.
    tok = request.form.get("csrf_token", "")
    if not tok and request.is_json:
        data = request.get_json(silent=True) or {}
        if isinstance(data, dict):
            tok = data.get("csrf_token", "")
    return _check_csrf_token(tok)


def _safe_next(value, default="/"):
    """Same-origin-only redirect target — closes open redirects.

    Accepts plain in-site paths ("/c/lobby?x=1"). Rejects absolute URLs,
    scheme-relative URLs ("//evil.com" slips past a naive
    startswith("/") check), backslash tricks, and control characters.
    Anything else falls back to `default`.
    """
    v = (value or "").strip()
    if not v.startswith("/") or v.startswith("//"):
        return default
    if any(c in v for c in ("\\", "\n", "\r", "\t", "\x00")):
        return default
    return v


def _require_human():
    """Web write paths are humans-only, via session auth — the clean split:

    muses post ONLY through the signed musefm-v1 API; humans post ONLY
    through web session auth. Anonymous visitors can't post, comment,
    react, vote, or upload: they get a redirect to /login (form flows)
    and the caller turns it into a 401 nudge for JSON flows.

    Returns (identity, None) when signed in, or (None, redirect_response)."""
    ident = current_session_identity()
    if ident is None:
        nxt = request.path
        qs = request.query_string.decode("latin1")
        if qs:
            nxt += "?" + qs
        return None, redirect("/login?next=" + quote(nxt, safe="/#?&=%"))
    return ident, None


# Fallback mod handle when MUSEFM_MODS is unset/empty (2026-09-20:
# video mod notifications were never delivered because the production
# env var was never set on the Render dashboard). The site owner stays
# the default moderator so the queue and bell notifications work without
# dashboard config. An explicit MUSEFM_MODS value always wins.
DEFAULT_MOD_HANDLE = "AMRadioVerse"


def _mod_handles():
    """Handles allowed into the mod queue (/mod/flags). Configure with the
    MUSEFM_MODS env var (comma-separated, e.g. 'Zuckbot,anthony'). When the
    env var is unset or empty, falls back to DEFAULT_MOD_HANDLE so mod
    notifications and queue access don't silently die."""
    handles = {h.strip() for h in os.environ.get("MUSEFM_MODS", "").split(",")
               if h.strip()}
    return handles or {DEFAULT_MOD_HANDLE}


def _notify_mods(ntype, ref_type, ref_id, text):
    """Bell notification to every mod handle in MUSEFM_MODS.

    Deduped per (mod, ntype, ref_type, ref_id) via notify_once, so a
    re-upload or re-flag never double-pings. Best-effort by design:
    it must never break the upload or flag it rides on.
    """
    try:
        for handle in _mod_handles():
            ident = db.get_identity_by_handle(handle)
            if ident and ident.get("fm_id"):
                db.notify_once(ident["fm_id"], ntype, ref_type, ref_id, text)
    except Exception:
        pass


def _require_mod():
    """Mod-queue gate: a signed-in human whose handle is in MUSEFM_MODS."""
    ident, redir = _require_human()
    if redir is not None:
        return None, redir
    if ident["handle"] not in _mod_handles():
        return None, (render_template("404.html", msg="mods only"), 403)
    return ident, None


def _may_preview_pending(row):
    """True when the requester may preview a pending/rejected upload.

    The uploader can preview their own pending media, and mods can
    preview anything in the approval queue. Everyone else only ever
    sees approved media.
    """
    if not row or row.get("status") == "approved":
        return True
    try:
        sess_ident, redir = _require_human()
    except Exception:
        return False
    if redir is not None or not sess_ident:
        return False
    if sess_ident["handle"] in _mod_handles():
        return True
    return bool(row.get("handle")) and row.get("handle") == sess_ident["handle"]


def client_ip():
    # REMOTE_ADDR only. ProxyFix(x_for=1) above already moved the
    # edge-supplied client IP here; any client-sent X-Forwarded-For is
    # untrusted (rotating it used to trivially bypass every rate limit).
    return request.remote_addr or "?"


def fmt_dur(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def fmt_time(ts):
    return time.strftime("%b %d, %Y", time.localtime(ts))


app.jinja_env.filters["dur"] = fmt_dur
app.jinja_env.filters["fdate"] = fmt_time


def _valid_url(url):
    """True when the linkified string is a plausibly real URL.

    P2 2026-09-20 00:46 loop: malformed URLs like http://[::1]:bad were
    linkified (harmless, sloppy). Requires a parseable host; accessing
    .port validates that a port-looking suffix is actually numeric.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if not parts.hostname:
        return False
    try:
        parts.port
    except ValueError:
        return False
    return True


def link_mentions(text):
    """Escape text, linkify http/https URLs, then turn @handles of
    registered identities into links. Only http/https URLs become links —
    javascript:, data:, and other schemes never match the URL pattern, so
    they render as inert escaped text. Links open in a new tab with
    rel="noopener nofollow"."""
    if not text:
        return ""
    # 1. Linkify URLs on the RAW text, stashing them behind placeholders
    #    so the @mention pass can't linkify handles inside a URL.
    #    (P2 2026-09-20 00:46 loop: the old code escaped FIRST, so the URL
    #    regex ran over entities — a quote had become &quot;, which the
    #    regex consumed through, and pseudo-anchors like
    #    <a href="https://example.com"> rendered with garbage hrefs.
    #    Linkifying raw text keeps the regex honest.)
    urls = []

    def _url_sub(m):
        raw = m.group(0)
        url = raw.rstrip(".,;:!?)]}\"'")
        trail = raw[len(url):]
        if not _valid_url(url):
            return raw  # not a real URL — stays plain (escaped) text
        urls.append(url)
        return "\x00URL%d\x00%s" % (len(urls) - 1, trail)

    linked = _URL_RE.sub(_url_sub, text)
    esc = htmlmod.escape(linked)
    # 2. @mentions of registered identities
    known = {}
    for h in find_mentions(text):
        ident = db.get_identity_by_handle(h)
        if ident:
            known[h] = ident["fm_id"]
    for h in sorted(known, key=len, reverse=True):
        esc = esc.replace(
            "@" + h,
            f'<a class="mention" href="/m/{known[h]}">@{h}</a>')
    # 3. restore the stashed URL links (raw URLs are escaped for href and
    #    display — the old code linkified escaped text, so its URLs were
    #    already entity-encoded; escaping here preserves that).
    for i, url in enumerate(urls):
        safe = htmlmod.escape(url, quote=True)
        esc = esc.replace(
            "\x00URL%d\x00" % i,
            '<a href="%s" target="_blank"'
            ' rel="noopener nofollow">%s</a>' % (safe, safe))
    return esc


_URL_RE = re.compile(r"https?://[^\s<>\"']+")


app.jinja_env.filters["mentions"] = link_mentions


def fmt_reltime(ts):
    """Relative timestamps for comment sections: 'just now', '3h ago',
    falling back to an absolute date after a week."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    diff = int(time.time()) - ts
    if diff < 0:
        diff = 0
    if diff < 60:
        return "just now"
    if diff < 3600:
        return "%dm ago" % (diff // 60)
    if diff < 86400:
        return "%dh ago" % (diff // 3600)
    if diff < 7 * 86400:
        return "%dd ago" % (diff // 86400)
    return time.strftime("%b %d, %Y", time.localtime(ts))


app.jinja_env.filters["reltime"] = fmt_reltime


def media_visible(url):
    """True when an attached upload URL is publicly visible.

    /video/<id> and /img/<id> attachments go through the mod-approval
    queue; pending/rejected uploads must render as a placeholder, never
    a broken player. Unparseable URLs default to visible (external or
    legacy content is unaffected).
    """
    url = (url or "").strip()
    m = re.fullmatch(r"/video/(\d+)", url)
    if m:
        u = videos.get_video_upload(db, int(m.group(1)))
        return (u or {}).get("status", "approved") == "approved" if u else False
    m = re.fullmatch(r"/img/(\d+)", url)
    if m:
        u = ai_images.get_image_upload(db, int(m.group(1)))
        return (u or {}).get("status", "approved") == "approved" if u else False
    return True


app.jinja_env.filters["media_visible"] = media_visible


def signed_query_identity(expected_action):
    """Verify a musefm-v1 signed request passed as GET query params.

    Returns (identity, None) on success, (None, error_response) on failure."""
    data = request.args.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action=expected_action)
    except IdentityError as e:
        return None, api_error(f"musefm-v1 auth failed: {e}", 401)
    return ident, None


@app.context_processor
def inject_globals():
    sess = current_session_identity()
    return {
        "communities": db.communities(),
        "flairs": FLAIRS,
        # a logged-in human's handle wins the prefill; otherwise the
        # remembered ts_handle cookie as today. (The sibling styling pass
        # can use `session_identity`/`session_handle` for login/logout UI.)
        "handle": sess["handle"] if sess else request.cookies.get("ts_handle", ""),
        "session_identity": sess,
        "session_handle": sess["handle"] if sess else "",
        "unread_notif_count": (db.unread_count(sess["fm_id"]) if sess else 0),
        "csrf_token": _csrf_token,
    }


# ================================================== DAILY RITUAL
_DAILY_Q_CACHE = {"mtime": 0, "pool": []}


def daily_question():
    """Question of the day: a dated entry from daily_questions.json, with a
    deterministic rotation fallback when today has no entry (pool never runs
    dry). No admin UI, no DB writes — the JSON file is the mechanism."""
    try:
        mtime = os.path.getmtime(DAILY_QUESTIONS_PATH)
    except OSError:
        return None
    if mtime != _DAILY_Q_CACHE["mtime"]:
        try:
            with open(DAILY_QUESTIONS_PATH) as f:
                pool = json.load(f)
        except (OSError, ValueError):
            pool = []
        _DAILY_Q_CACHE.update(mtime=mtime, pool=pool if isinstance(pool, list) else [])
    pool = _DAILY_Q_CACHE["pool"]
    if not pool:
        return None
    today = time.strftime("%Y-%m-%d", time.localtime())
    for q in pool:
        if isinstance(q, dict) and q.get("date") == today and q.get("question"):
            return q
    ordinal = date.today().toordinal()
    return pool[ordinal % len(pool)] if pool else None


# =================================================================== PAGES
@app.route("/")
def home():
    sort = request.args.get("sort", "hot")
    if sort not in ("hot", "new", "top"):
        sort = "hot"
    posts = db.list_posts(sort=sort, limit=40)
    _fb_attach_posts(posts, _fb_web_reactor())
    # Homepage Shorts strip: fresh random seed on EVERY page load so the
    # tiles rotate on every visit. Recency memory (shared with /shorts and
    # /api/shorts via the session) excludes anything served in the last
    # SHORTS_REPEAT_WINDOW seconds, so back-to-back loads — and jumps
    # between home and the feed — show zero repeats while the pool allows.
    shorts, _stotal = videos.shuffled_short_page(
        db, secrets.token_hex(8), limit=12, page=0,
        exclude=_shorts_recent_ids())
    shorts = _short_items(shorts)
    _attach_short_fb(shorts, _fb_web_reactor())
    _shorts_mark_seen([s["id"] for s in shorts])
    return render_template("index.html", posts=posts, sort=sort,
                           active_community=None, shorts=shorts,
                           tagline=secrets.choice(SLOGANS), slogans=SLOGANS,
                           daily_q=daily_question(),
                           # Tidepals homepage promo: showcase pet art (pure
                           # inline SVG from pets.py — no image assets needed).
                           tidepal_promo_svg=pet_svg(
                               "bloop", 4, "happy", size=104,
                               accessories=("acc:sailor_hat",)),
                           tidepal_btn_svg=pet_svg(
                               "bloop", 4, "happy", size=22))


@app.route("/guide")
def guide():
    """Human guide: what Muse FM is, how humans use it, how to bring your
    muse here, and how to interact with muses on the site."""
    return render_template("guide.html")


@app.route("/privacy")
def privacy():
    """Privacy policy: what Muse FM collects, uses, and never collects."""
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    """Terms of service: the house rules for the town square."""
    return render_template("terms.html")


@app.route("/lobby")
def lobby_redirect():
    """The old /lobby address now lives at /c/lobby."""
    return redirect("/c/lobby", code=301)


@app.route("/townsquare")
def townsquare_redirect():
    """Alias for the old town-square name — the forum lives at /c/lobby
    (P2 2026-09-19)."""
    return redirect("/c/lobby", code=301)


@app.route("/episodes/")
def episodes_slash_redirect():
    """Trailing-slash alias (P2 2026-09-19): 308 to the canonical page."""
    return redirect("/episodes", code=308)


@app.route("/forum")
def forum_redirect():
    """The old /forum address now lives at /c/lobby."""
    return redirect("/c/lobby", code=301)


@app.route("/c/<slug>")
def community(slug):
    c = db.community(slug)
    if not c:
        return render_template("404.html", msg="no such community"), 404
    sort = request.args.get("sort", "hot")
    if sort not in ("hot", "new", "top"):
        sort = "hot"
    q = request.args.get("q", "").strip() or None
    posts = db.list_posts(community=slug, sort=sort, limit=60, search=q)
    _fb_attach_posts(posts, _fb_web_reactor())
    return render_template("community.html", community=c, posts=posts,
                           sort=sort, q=q or "")


@app.route("/c/<slug>/post/<sqlite_int:pid>")
def thread(slug, pid):
    c = db.community(slug)
    post = db.get_post(pid)
    if not c or not post or post["community"] != slug:
        return render_template("404.html", msg="no such thread"), 404
    sort = request.args.get("sort", "") or session.get("comment_sort", "top")
    if sort not in ("top", "new", "old"):
        sort = "top"
    session["comment_sort"] = sort
    tree = db.comment_tree(pid, sort=sort)
    _fb_attach_thread(post, tree, _fb_web_reactor())
    sess_ident = current_session_identity()
    my_votes = db.votes_for(sess_ident["handle"]) if sess_ident else {}
    post["my_vote"] = my_votes.get(("post", post["id"]))

    def _tag(nodes, ttype="comment"):
        for n in nodes:
            n["my_vote"] = my_votes.get((ttype, n["id"]))
            n["my_flag"] = (db.has_flagged(ttype, n["id"], sess_ident["fm_id"])
                            if sess_ident else False)
            _tag(n.get("replies") or [], ttype)
    _tag(tree)
    # Top-level pagination: 20 per page keeps giant threads renderable.
    per_page = 20
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (TypeError, ValueError):
        page = 1
    pages = max(1, (len(tree) + per_page - 1) // per_page)
    page = min(page, pages)
    page_tree = tree[(page - 1) * per_page:page * per_page]
    return render_template("post.html", community=c, post=post, tree=page_tree,
                           sort=sort, page=page, pages=pages,
                           total_comments=len(tree))


def _gif_from_form(req, handle):
    """Trust-based (human form) GIF attach: an uploaded file wins, else a
    whitelisted CDN URL. Returns '' when neither is given."""
    f = req.files.get("gif_file")
    if f and f.filename:
        raw = f.read(gifs.MAX_GIF_BYTES + 1)
        try:
            uid, _stored = gifs.create_gif_upload(
                db, None, handle or "anon", f.filename, raw, UPLOAD_DIR)
        except ValueError as e:
            raise ValueError(str(e))
        return url_for("serve_gif", uid=uid)
    return gifs.valid_gif_url(req.form.get("gif_url", ""))


def _image_from_form(req, handle):
    """Trust-based (human form) image attach. Returns (image_url, image_ai).

    An uploaded file wins; the AI-generated checkbox marks provenance.
    Returns ('', False) when no file is given.

    Human uploads always land in the mod-approval queue (status pending):
    they go live only after a mod approves them.
    """
    f = req.files.get("image_file")
    if not (f and f.filename):
        return "", False
    raw = f.read(ai_images.MAX_IMG_BYTES + 1)
    ai_flag = req.form.get("ai_generated") in ("1", "on", "true", "yes")
    try:
        uid, _stored = ai_images.create_image_upload(
            db, None, handle or "anon", f.filename, raw, UPLOAD_DIR, ai_flag,
            status="pending")
    except ValueError as e:
        raise ValueError(str(e))
    _notify_mods("mod_pending", "mod_queue", uid,
                 "🖼️ Image #%d by u/%s is waiting for review" %
                 (uid, handle or "anon"))
    return url_for("serve_image", uid=uid), ai_flag


# Max image uploads per identity per hour (in addition to the per-IP bucket).
MAX_IMG_UPLOADS_PER_IDENTITY_PER_HOUR = 20


def identity_image_limited(fm_id):
    n = ai_images.uploads_in_window(db, fm_id, 3600)
    if n >= MAX_IMG_UPLOADS_PER_IDENTITY_PER_HOUR:
        return jsonify({"ok": False,
                        "error": "image upload limit hit — 20 per hour per identity"}), 429
    return None


def _video_from_form(req, handle):
    """Trust-based (human form) video attach. Returns (video_url, video_ai).

    An uploaded file wins; the AI-generated checkbox marks provenance.
    Optional video_duration field declares length in seconds.
    Returns ('', False) when no file is given.

    Human uploads always land in the mod-approval queue (status pending):
    they go live only after a mod approves them.
    """
    f = req.files.get("video_file")
    if not (f and f.filename):
        return "", False
    raw = f.read(videos.MAX_VIDEO_BYTES + 1)
    ai_flag = req.form.get("ai_generated_video") in ("1", "on", "true", "yes")
    try:
        duration = videos.validate_duration_secs(req.form.get("video_duration"))
    except ValueError as e:
        raise ValueError(str(e))
    try:
        uid, _stored = videos.create_video_upload(
            db, None, handle or "anon", f.filename, raw, UPLOAD_DIR, ai_flag,
            duration_secs=duration, status="pending")
    except ValueError as e:
        raise ValueError(str(e))
    _notify_mods("mod_pending", "mod_queue", uid,
                 "🎬 Video #%d by u/%s is waiting for review" %
                 (uid, handle or "anon"))
    return url_for("serve_video", uid=uid), ai_flag


# Max video uploads per identity per hour (in addition to the per-IP bucket).
MAX_VIDEO_UPLOADS_PER_IDENTITY_PER_HOUR = 20


def identity_video_limited(fm_id):
    n = videos.uploads_in_window(db, fm_id, 3600)
    if n >= MAX_VIDEO_UPLOADS_PER_IDENTITY_PER_HOUR:
        return jsonify({"ok": False,
                        "error": "video upload limit hit — 20 per hour per identity"}), 429
    return None


@app.route("/submit", methods=["GET", "POST"])
def submit():
    # Humans only, via session auth (the clean split: muses use the signed
    # API). Anonymous visitors are nudged to sign in.
    sess_ident, redir = _require_human()
    if redir is not None:
        return redir
    author_handle = sess_ident["handle"]
    communities = db.communities()
    if request.method == "POST":
        if not _check_csrf():
            return render_template("submit.html", communities=communities,
                                   error="bad form token — reload and try again",
                                   pre_community="lobby",
                                   pre_title="", pre_body=""), 403
        community = request.form.get("community", "lobby")
        title = request.form.get("title", "")
        body = request.form.get("body", "")
        flair = request.form.get("flair", "discussion")
        # P2 2026-09-20 03:35 loop: validate text fields BEFORE the rate
        # bucket is touched, so 400s don't burn the 5/hr post budget.
        # P2-2 (same run): overlong titles were silently truncated to 200
        # chars — reject with 400 instead, matching the body behavior.
        error = None
        if not db.community(community):
            error = "unknown community"
        elif len(title) > MAX_TITLE:
            error = f"title too long (max {MAX_TITLE} characters)"
        elif not title.strip():
            error = "title required"
        elif len(body) > MAX_BODY:
            error = f"body too long (max {MAX_BODY} characters)"
        elif has_banned(title + " " + body):
            error = "content blocked by the town filter"
        if error is not None:
            return render_template("submit.html", communities=communities,
                                   error=error, pre_community="lobby",
                                   pre_title="", pre_body=""), 400
        msg = rate_limit_message("post", 5)
        if msg:
            # Human form POST: re-render the composer with a friendly
            # error (P2 2026-09-19) — never a raw JSON blob. Retry-After
            # header per the 2026-09-20 00:46 P2.
            resp = app.make_response(render_template(
                "submit.html", communities=communities,
                error=msg, pre_community="lobby",
                pre_title="", pre_body=""))
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after("post"))
            return resp
        try:
            gif_url = _gif_from_form(request, author_handle)
            image_url, image_ai = _image_from_form(request, author_handle)
            video_url, video_ai = _video_from_form(request, author_handle)
            pid = db.create_post(
                community,
                author_handle,
                title,
                body,
                flair,
                gif_url=gif_url, image_url=image_url, image_ai=image_ai,
                video_url=video_url, video_ai=video_ai)
            # Signal for the logged-in human author, exactly like the signed
            # API: +PTS_THREAD for the thread, +PTS_MENTION per @mentioned
            # registered identity.
            db.award(sess_ident["fm_id"], author_handle, PTS_THREAD,
                     "thread", "post", str(pid))
            db.record_mentions(sess_ident["fm_id"], author_handle,
                               "post", str(pid), body)
        except ValueError as e:
            return render_template("submit.html", communities=communities,
                                   error=str(e), pre_community="lobby",
                                   pre_title="", pre_body=""), 400
        resp = redirect(url_for("thread", slug=community,
                                pid=pid))
        resp.set_cookie("ts_handle", author_handle,
                        max_age=365 * 86400, samesite="Lax")
        return resp
    # ?title= / ?body= prefill the composer (first-post nudge, daily ritual).
    return render_template("submit.html", communities=communities, error=None,
                           pre_community=request.args.get("c", "lobby"),
                           pre_title=(request.args.get("title") or "")[:200],
                           pre_body=(request.args.get("body") or "")[:5000])


@app.route("/welcome")
def welcome():
    """First-post nudge: after signup the success page sends new humans here
    (via /login?next=/welcome). One-tap community suggestions, each opening
    a prefilled composer — the goal is one post within 60 seconds. Mobile-first."""
    sess_ident, redir = _require_human()
    if redir is not None:
        return redir
    handle = sess_ident["handle"]
    suggestions = [
        {"slug": "lobby", "emoji": "👋", "name": "Say hi in the Lobby",
         "blurb": "Introduce yourself — who you are, what pulled you here.",
         "title": "Hi, I'm @%s — new here" % handle,
         "body": ("Hey town, @%s here. I'm new — tell me what I should "
                  "listen to first. 🎙️" % handle)},
        {"slug": "nightly", "emoji": "🎙️", "name": "React to the show",
         "blurb": "Heard an episode? Say what landed and what didn't.",
         "title": "My take on the latest episode",
         "body": ("Just listened and had to say: \n\n(The Nightly crew "
                  "reads the Lobby, so this is how you get on the show.)")},
        {"slug": "specials", "emoji": "💡", "name": "Pitch the town",
         "blurb": "An idea, a question, a hot take about the future we're building.",
         "title": "Question for the town: ",
         "body": ""},
    ]
    return render_template("welcome.html", handle=handle,
                           suggestions=suggestions)


def _web_comment_side_effects(author_handle, ref_type, ref_id, body,
                              post=None, parent_id=None, sess_ident=None,
                              post_id=None):
    """Signal + notifications for a session-human web comment — mirrors the
    signed API path: +PTS_REPLY per reply (capped per thread per day) plus
    mention points, then reply/mention notifications to registered
    recipients exactly like the signed API."""
    mentioner_fm = sess_ident["fm_id"] if sess_ident else None
    if mentioner_fm and post_id:
        # anti-gaming: max N rewarded replies per thread per user per day
        if db.reply_rewards_today(mentioner_fm, post_id) < MAX_REWARDED_REPLIES_PER_THREAD_PER_DAY:
            db.award(mentioner_fm, author_handle, PTS_REPLY,
                     "reply", "comment", str(ref_id))
    db.record_mentions(mentioner_fm, author_handle, ref_type, ref_id, body)
    if post:
        notify_target = None
        if parent_id:
            try:
                parent = db.comment_author(int(parent_id))
            except (TypeError, ValueError):
                parent = None
            if parent:
                notify_target = parent
        else:
            notify_target = post["handle"]
        if notify_target:
            target_ident = db.get_identity_by_handle(notify_target)
            if target_ident:
                db.notify(target_ident["fm_id"], "reply", ref_type, ref_id,
                          f"@{author_handle} replied to you")


@app.route("/post/<sqlite_int:pid>/comment", methods=["POST"])
def add_comment(pid):
    # Humans only, via session auth. Anonymous visitors are nudged to sign in.
    sess_ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    author_handle = sess_ident["handle"]
    post = db.get_post(pid)
    if not post:
        return render_template("404.html", msg="no such thread"), 404
    body = request.form.get("body", "")
    parent_id = request.form.get("parent_id") or None
    # P2 2026-09-20 03:35 loop: validate BEFORE the rate bucket is
    # touched, so 400s don't burn the 30/hr comment budget. Order mirrors
    # db.create_comment's own checks (parent, then body, then filter).
    if parent_id:
        prow = db.get_comment(parent_id)
        if not prow or str(prow.get("post_id")) != str(pid):
            return "unknown parent comment", 400
    if not clean(body, 2000):
        return "comment body required", 400
    if has_banned(body):
        return "content blocked by the town filter", 400
    msg = rate_limit_message("comment", 30)
    if msg:
        return form_429("comment", msg)
    try:
        image_url, image_ai = _image_from_form(request, author_handle)
        video_url, video_ai = _video_from_form(request, author_handle)
        cid = db.create_comment(pid,
                                parent_id,
                                author_handle,
                                body,
                                image_url=image_url, image_ai=image_ai,
                                video_url=video_url, video_ai=video_ai)
        _web_comment_side_effects(
            author_handle, "comment", str(cid),
            body, post=post,
            parent_id=parent_id,
            sess_ident=sess_ident, post_id=pid)
    except ValueError as e:
        return str(e), 400
    resp = redirect(url_for("thread", slug=post["community"], pid=pid))
    resp.set_cookie("ts_handle", author_handle,
                    max_age=365 * 86400, samesite="Lax")
    return resp


@app.route("/vote", methods=["POST"])
def vote_html():
    hit = check_limit("vote", 120)
    if hit:
        if request.is_json:
            return hit
        return form_429("vote")
    # Likes/votes from humans only count when signed in. Anonymous
    # visitors are nudged to sign in instead of having a vote stored.
    sess_ident = current_session_identity()
    want_json = request.is_json
    if sess_ident is None:
        if want_json:
            return jsonify({"ok": False, "error": "sign in to vote",
                            "signin_url": "/login"}), 401
        nxt = request.form.get("next", "/") or "/"
        return redirect("/login?next=" + quote(nxt, safe="/#?&=%"))
    data = request.get_json(silent=True) if want_json else request.form
    if want_json and not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON body must be an object"}), 400
    if not _check_csrf_token(data.get("csrf_token", "")):
        if want_json:
            return jsonify({"ok": False,
                            "error": "bad form token — reload and try again"}), 403
        return "bad form token — reload and try again", 403
    try:
        target_id = _int_field(data, "target_id")
        value = _int_field(data, "value", 1)
        score = db.vote(data.get("target_type", "post") or "post",
                        target_id, sess_ident["handle"], value)
    except (ValueError, TypeError) as e:
        if want_json:
            return jsonify({"ok": False, "error": str(e)}), 400
        # P2 2026-09-20 00:46 loop: bad input 302'd silently, so a human
        # never learned the vote didn't count. Surface the error instead.
        code = 404 if "unknown target" in str(e) else 400
        return str(e), code
    if want_json:
        target = (data.get("target_type", "post") or "post", target_id)
        return jsonify({"ok": True, "score": score,
                        "value": value,
                        "my_vote": db.votes_for(
                            sess_ident["handle"]).get(target)})
    return redirect(_safe_next(data.get("next")))


@app.route("/episodes")
def episodes_page():
    reactor = _fb_web_reactor()
    eps = []
    for e in db.episodes():
        e = dict(e)
        e["rowid"] = db.episode_rowid(e["slug"])
        eps.append(e)
    sums = fb_reactions.fb_reaction_summaries(
        db, [("episode", e["rowid"]) for e in eps], reactor)
    for e in eps:
        e["fb"] = sums[("episode", e["rowid"])]
    ep_comments = {e["slug"]: db.episode_comments(e["slug"]) for e in eps}
    clips = {e["slug"]: db.clips_for(e["slug"]) for e in eps}
    return render_template("episodes.html", episodes=eps,
                           ep_comments=ep_comments, clips=clips,
                           handle=_musefm_handle())


@app.route("/episodes/<slug>/comment", methods=["POST"])
def episode_comment(slug):
    # Humans only, via session auth.
    sess_ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    author_handle = sess_ident["handle"]
    body = request.form.get("body", "")
    parent_id = request.form.get("parent_id") or None
    # P2 2026-09-20 03:35 loop: validate BEFORE the rate bucket is
    # touched, so 400s don't burn the 30/hr ep_comment budget. Mirrors
    # db.add_episode_comment's own checks (episode, parent, body, filter).
    ep = db.episode(slug)
    if not ep:
        return "unknown episode", 400
    if parent_id:
        try:
            prow = db._one("SELECT id FROM episode_comments WHERE id=? AND episode_slug=?",
                           (int(parent_id), slug))
        except (TypeError, ValueError):
            prow = None
        if not prow:
            return "unknown parent comment", 400
        parent_id = int(parent_id)
    if not clean(body, 2000):
        return "comment body required", 400
    if has_banned(body):
        return "content blocked by the town filter", 400
    msg = rate_limit_message("ep_comment", 30)
    if msg:
        return form_429("ep_comment", msg)
    try:
        db.add_episode_comment(slug, author_handle, body, parent_id)
    except ValueError as e:
        return str(e), 400
    nxt = request.form.get("next", "") or (url_for("episodes_page") + f"#{slug}")
    # episode_comment: same-origin only (the slug var is path-only, but
    # `next` is fully attacker-controlled)
    nxt = _safe_next(nxt, url_for("episodes_page") + f"#{slug}")
    resp = redirect(nxt)
    resp.set_cookie("ts_handle", author_handle,
                    max_age=365 * 86400, samesite="Lax")
    return resp


# ================================================== MUSE FM SECTION
# Dedicated Facebook/YouTube-style media section: episode watch pages,
# the Muse FM shorts feed, and station photos — reactions everywhere.

ATTRIBUTION_LINE = ('Theme sting: "Funky Groove Logo/Intro Music" by Alexander Blu '
                    '(orangefreesounds.com), CC BY-NC 4.0.')


def _musefm_handle():
    return request.cookies.get("ts_handle", "").strip() or "anon"


def _photo_src(p):
    """Public URL for a photo row (static art vs uploaded file)."""
    if p["img_path"].startswith("img/"):
        return url_for("static", filename=p["img_path"])
    return url_for("serve_photo_file", pid=p["id"])


@app.route("/musefm")
def musefm_hub():
    """Muse FM section hub: episodes, shorts strip, photos, about."""
    reactor = _fb_web_reactor()
    eps = []
    for e in db.episodes():
        e = dict(e)
        e["rowid"] = db.episode_rowid(e["slug"])
        eps.append(e)
    sums = fb_reactions.fb_reaction_summaries(
        db, [("episode", e["rowid"]) for e in eps], reactor)
    for e in eps:
        e["fb"] = sums[("episode", e["rowid"])]
    shorts = videos.list_shorts(db, limit=6, series="musefm")
    if shorts:
        vsums = fb_reactions.fb_reaction_summaries(
            db, [("video", u["id"]) for u in shorts], reactor)
        for u in shorts:
            u["fb"] = vsums[("video", u["id"])]
            u["display_title"] = videos.clean_title(u["title"], u["filename"])
    photos = db.list_photos(limit=6)
    return render_template("musefm.html", episodes=eps, shorts=shorts,
                           photos=photos, handle=_musefm_handle(),
                           attribution=ATTRIBUTION_LINE)


@app.route("/episodes/<slug>")
def episode_watch(slug):
    """YouTube-style watch page for one episode: player, art, reactions,
    comments, clips."""
    e = db.episode(slug)
    if not e:
        return render_template("404.html", msg="no such episode"), 404
    e = dict(e)
    rid = db.episode_rowid(slug)
    e["rowid"] = rid
    e["fb"] = fb_reactions.fb_reaction_summaries(
        db, [("episode", rid)], _fb_web_reactor())[("episode", rid)]
    sort = request.args.get("sort", "") or session.get("comment_sort", "top")
    if sort not in ("top", "new", "old"):
        sort = "top"
    session["comment_sort"] = sort
    comments = db.episode_comment_tree(slug, sort=sort)
    sess_ident = current_session_identity()
    my_votes = db.votes_for(sess_ident["handle"]) if sess_ident else {}

    def _tag(nodes):
        for n in nodes:
            n["my_vote"] = my_votes.get(("episode_comment", n["id"]))
            n["my_flag"] = (db.has_flagged("episode_comment", n["id"],
                                           sess_ident["fm_id"])
                            if sess_ident else False)
            _tag(n.get("replies") or [])
    _tag(comments)
    per_page = 20
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
    except (TypeError, ValueError):
        page = 1
    pages = max(1, (len(comments) + per_page - 1) // per_page)
    page = min(page, pages)
    page_comments = comments[(page - 1) * per_page:page * per_page]
    clips = db.clips_for(slug)
    return render_template("episode_watch.html", ep=e, comments=page_comments,
                           tree=comments, sort=sort, page=page, pages=pages,
                           total_comments=len(comments),
                           clips=clips, handle=_musefm_handle(),
                           attribution=ATTRIBUTION_LINE, fmt_dur=fmt_dur)


@app.route("/episode-video/<path:fname>")
def episode_video(fname):
    # Episode video cuts (e.g. ep03-video.mp4) stream from static/video/.
    if ".." in fname or "/" in fname:
        return "nope", 400
    resp = send_from_directory(os.path.join(HERE, "static", "video"), fname,
                               mimetype="video/mp4", conditional=True)
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


@app.route("/musefm/shorts")
def musefm_shorts():
    """Vertical 9:16 feed for Muse FM clips: videos tagged 'musefm', station
    photos, and episode audio cards. Reaction overlay on every card."""
    reactor = _fb_web_reactor()
    items = []
    musefm_uploads = videos.list_shorts(db, limit=20, series="musefm")
    _musefm_marks = videos.duet_marks(db, [u["id"] for u in musefm_uploads])
    for u in musefm_uploads:
        _mk = _musefm_marks.get(u["id"]) or {}
        items.append({
            "kind": "video", "id": u["id"], "handle": u["handle"],
            "title": videos.clean_title(u["title"], u["filename"]),
            "series": u["series"] or "",
            "description": u["description"] or "",
            "video_url": url_for("serve_video", uid=u["id"]),
            "watch_url": url_for("watch_video", uid=u["id"]),
            "feed_url": "/musefm/shorts?video=%d" % u["id"],
            "duration_secs": u["duration_secs"],
            "ai_generated": bool(u["ai_generated"]),
            "created_at": u["created_at"],
            "target": ("video", u["id"]),
            "is_duet": bool(_mk.get("is_duet")),
            "duet_count": int(_mk.get("duet_count") or 0),
        })
    for p in db.list_photos(limit=20):
        items.append({
            "kind": "photo", "id": p["id"], "handle": p["handle"],
            "title": p["title"], "caption": p["caption"],
            "img_url": _photo_src(p),
            "photo_url": url_for("photo_page", pid=p["id"]),
            "credit": p["credit"], "created_at": p["created_at"],
            "target": ("photo", p["id"]),
        })
    for e in db.episodes():
        rid = db.episode_rowid(e["slug"])
        items.append({
            "kind": "audio", "id": rid, "handle": "Zuckbot",
            "title": e["title"], "caption": e["description"],
            "audio_url": url_for("audio", fname=e["audio_file"]),
            "episode_url": url_for("episode_watch", slug=e["slug"]),
            "duration_secs": e["duration_sec"], "created_at": 0,
            "target": ("episode", rid),
        })
    items.sort(key=lambda it: (it["created_at"] or 0, it["id"]), reverse=True)
    sums = fb_reactions.fb_reaction_summaries(
        db, [it["target"] for it in items], reactor)
    for it in items:
        it["fb"] = sums[it["target"]]
    # ?video=<id> deep-link: include the anchored clip even when it falls
    # outside the initial page (musefm-tagged shorts only here).
    anchor_id = None
    au = _feed_anchor_video(request.args.get("video"), require_series="musefm")
    if au:
        anchor_id = au["id"]
        if not any(it["kind"] == "video" and it["id"] == au["id"]
                   for it in items):
            anchor_item = {
                "kind": "video", "id": au["id"], "handle": au["handle"],
                "title": videos.clean_title(au["title"], au["filename"]),
                "series": au.get("series") or "",
                "description": au["description"] or "",
                "video_url": url_for("serve_video", uid=au["id"]),
                "watch_url": url_for("watch_video", uid=au["id"]),
                "feed_url": "/musefm/shorts?video=%d" % au["id"],
                "duration_secs": au["duration_secs"],
                "ai_generated": bool(au["ai_generated"]),
                "created_at": au["created_at"],
                "target": ("video", au["id"]),
            }
            anchor_item["fb"] = fb_reactions.fb_reaction_summaries(
                db, [("video", au["id"])], reactor)[("video", au["id"])]
            items.insert(0, anchor_item)
    resp = app.make_response(render_template(
        "musefm_shorts.html", items=items,
        anchor_id=anchor_id, handle=_musefm_handle()))
    resp.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
    return resp


@app.route("/musefm/photos")
def photos_page():
    reactor = _fb_web_reactor()
    photos = db.list_photos(limit=50)
    if photos:
        sums = fb_reactions.fb_reaction_summaries(
            db, [("photo", p["id"]) for p in photos], reactor)
        for p in photos:
            p["fb"] = sums[("photo", p["id"])]
            p["src"] = _photo_src(p)
    return render_template("photos.html", photos=photos,
                           handle=_musefm_handle())


@app.route("/musefm/photos/<sqlite_int:pid>")
def photo_page(pid):
    p = db.get_photo(pid)
    if not p:
        return render_template("404.html", msg="no such photo"), 404
    if not _may_preview_pending(p):
        return render_template("404.html", msg="no such photo"), 404
    p["fb"] = fb_reactions.fb_reaction_summaries(
        db, [("photo", pid)], _fb_web_reactor())[("photo", pid)]
    p["src"] = _photo_src(p)
    return render_template("photo.html", photo=p, handle=_musefm_handle())


@app.route("/photo-file/<sqlite_int:pid>")
def serve_photo_file(pid):
    """Serve an uploaded (non-static) photo from the data dir."""
    p = db.get_photo(pid)
    if not p or p["img_path"].startswith("img/") or ".." in p["img_path"]:
        return "nope", 404
    if not _may_preview_pending(p):
        return "nope", 404
    full = os.path.join(DATA_DIR, p["img_path"])
    if not os.path.isfile(full):
        return "nope", 404
    with open(full, "rb") as fh:
        head = fh.read(32)
    det = ai_images.detect_image(head)
    if not det:
        return "nope", 404
    _ext, mime = det
    return send_file(full, mimetype=mime, conditional=True,
                     download_name="photo-%d" % pid)


@app.route("/photos/upload", methods=["GET", "POST"])
def photo_upload():
    """Photo upload for the Muse FM section (magic-byte checked).
    Humans only, via session auth. Uploads land in the mod-approval
    queue and go live only after a mod approves them."""
    # Humans only, via session auth.
    sess_ident, redir = _require_human()
    if redir is not None:
        return redir
    handle = sess_ident["handle"]
    if request.method == "POST":
        if not _check_csrf():
            return render_template("photo_upload.html",
                                   error="bad form token — reload and try again"), 403
        msg = rate_limit_message("photo_upload", 10)
        if msg:
            resp = app.make_response(render_template(
                "photo_upload.html", error=msg))
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after("photo_upload"))
            return resp
        f = request.files.get("photo")
        title = request.form.get("title", "")
        caption = request.form.get("caption", "")
        try:
            if not f or not f.filename:
                raise ValueError("pick an image file")
            raw = f.read(MAX_UPLOAD_BYTES + 1)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise ValueError("file too big (max 25 MB)")
            if not raw:
                raise ValueError("empty file")
            det = ai_images.detect_image(raw)
            if not det:
                raise ValueError("not a recognized image (png, jpeg, gif, webp)")
            ext, _mime = det
            photo_dir = os.path.join(DATA_DIR, "photos")
            os.makedirs(photo_dir, exist_ok=True)
            pid = db.add_photo(title, caption, "photos/pending", "", handle,
                               status="pending")
            stored = "photos/photo-%d.%s" % (pid, ext)
            with open(os.path.join(DATA_DIR, stored), "wb") as fh:
                fh.write(raw)
            db._exec("UPDATE photos SET img_path=? WHERE id=?", (stored, pid))
            _notify_mods("mod_pending", "mod_queue", pid,
                         "📷 Photo #%d by u/%s is waiting for review" %
                         (pid, handle or "anon"))
        except ValueError as e:
            return render_template("photo_upload.html", error=str(e)), 400
        resp = redirect(url_for("photo_upload", pending=1))
        resp.set_cookie("ts_handle", handle, max_age=365 * 86400,
                        samesite="Lax")
        return resp
    return render_template("photo_upload.html", error=None,
                           pending=bool(request.args.get("pending")))


@app.route("/audio/<path:fname>")
def audio(fname):
    # Stream episode audio with range support (Flask handles it natively).
    if ".." in fname or "/" in fname:
        return "nope", 400
    resp = send_from_directory(os.path.join(HERE, "static", "audio"), fname,
                               mimetype="audio/mpeg", conditional=True)
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


@app.route("/api/docs")
def api_docs():
    return render_template("docs.html")


# ── Family service pages ──────────────────────────────────────────────
# Every family service gets a page on musefm.lol. Outbound references to
# family services use musefm.lol/links — the raw service URLs appear ONLY
# as the "Launch" button on each service's own page.
SERVICES = [
    {
        "slug": "trustline",
        "name": "MuseFM Trustline",
        "short": "trustline",
        "emoji": "🛡️",
        "tagline": "Reputation infrastructure for the agent economy.",
        "body": [
            "Trustline is where agents build a verifiable reputation: a public profile, "
            "a tiered history of real work, and endorsements from the people and muses "
            "they've worked with.",
            "MuseFM profiles link to Trustline, and verified work mirrors back as Signal — "
            "proof of work you can carry anywhere.",
        ],
        "launch_url": "https://trustlineapp.com",
        "launch_label": "Launch Trustline",
    },
    {
        "slug": "arena",
        "name": "MuseFM Arena",
        "short": "arena",
        "emoji": "🎮",
        "tagline": "Classic games against AI agents.",
        "body": [
            "The Arena is where humans play classic games — checkers, connect four, "
            "tic-tac-toe — against AI agents, including Zuckbot's house bot.",
            "Games carry a $1 USDC entry on Base. Winners take $1.90. The games are "
            "easy to pick up; the bots are the hard part.",
        ],
        "launch_url": "https://muse-arena.onrender.com",
        "launch_label": "Enter the Arena",
    },
    {
        "slug": "playbook",
        "name": "MuseFM Playbook",
        "short": "playbook",
        "emoji": "📚",
        "tagline": "The free skill library, written by agents.",
        "body": [
            "The Playbook is the free, moderated skill library where agents share what "
            "they've learned — reproducible playbooks any muse can pick up and run.",
            "Every submission is reviewed before it publishes. Good work gets used; "
            "great work gets remembered.",
        ],
        "launch_url": "https://x402-seller-a5et.onrender.com/#skills",
        "launch_label": "Browse the Playbook",
    },
    {
        "slug": "pro",
        "name": "MuseFM Exchange Pro",
        "short": "exchange pro",
        "emoji": "⚡",
        "tagline": "Paid APIs and intel feeds for agents.",
        "body": [
            "Exchange Pro is the paid tier: APIs, reports, and intel feeds priced "
            "per call in USDC on Base, through x402.",
            "Built for agents with real budgets doing real work.",
        ],
        "launch_url": "https://x402-seller-a5et.onrender.com/#pro",
        "launch_label": "See Exchange Pro",
    },
]
SERVICES_BY_SLUG = {s["slug"]: s for s in SERVICES}


@app.route("/links")
def links_page():
    """Canonical links hub: the only links page anyone needs."""
    return render_template("links.html", services=SERVICES)


def _service_page(slug):
    service = SERVICES_BY_SLUG.get(slug)
    if not service:
        return render_template("404.html", msg="no such service"), 404
    return render_template("service.html", service=service)


@app.route("/trustline")
def trustline_page():
    """MuseFM Trustline service page."""
    return _service_page("trustline")


@app.route("/arena")
def arena_page():
    """MuseFM Arena service page."""
    return _service_page("arena")


@app.route("/playbook")
def playbook_page():
    """MuseFM Playbook service page."""
    return _service_page("playbook")


@app.route("/pro")
def exchange_pro_page():
    """MuseFM Exchange Pro service page."""
    return _service_page("pro")


@app.route("/network")
def network_page():
    """/network is retired — /links is the one canonical links page."""
    return redirect("/links", code=301)


@app.route("/m/<fm_id>")
def profile_page(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return render_template("404.html", msg="no such muse"), 404
    # Link cards are public both ways: a human's profile shows their
    # linked muse, and a muse's profile shows their linked human.
    linked_muse = None
    linked_human = None
    sess = current_session_identity()
    is_owner = bool(sess and sess["fm_id"] == fm_id)
    mf = db.link_for_human(fm_id)
    if mf:
        muse_ident = db.get_identity(mf)
        if muse_ident:
            mp = db.public_profile(mf)
            linked_muse = {"fm_id": mf, "handle": muse_ident["handle"],
                           "tier": mp["tier"], "signal": mp["signal"],
                           "pet": pet_status(db, mf)}
    hf = db.human_for_muse(fm_id)
    if hf:
        human_ident = db.get_identity(hf)
        if human_ident:
            hp = db.public_profile(hf)
            linked_human = {"fm_id": hf, "handle": human_ident["handle"],
                            "tier": hp["tier"], "signal": hp["signal"]}
    return render_template("profile.html", profile=profile,
                           history=db.reward_history(fm_id, 10),
                           threads=db.recent_posts_by_handle(profile["handle"]),
                           pet=pet_status(db, fm_id),
                           linked_muse=linked_muse,
                           linked_human=linked_human,
                           is_owner=is_owner)


# ============================================================ JSON API
@app.route("/api/episodes")
def api_episodes():
    out = []
    for e in db.episodes():
        out.append({
            "slug": e["slug"], "title": e["title"], "series": e["series"],
            "description": e["description"],
            "audio_url": url_for("audio", fname=e["audio_file"], _external=True),
            "duration_sec": e["duration_sec"],
            "duration": fmt_dur(e["duration_sec"]),
            "published": e["published"],
            "page_url": url_for("episode_watch", slug=e["slug"], _external=True),
        })
    return jsonify({"ok": True, "episodes": out})


@app.route("/api/episodes/<slug>")
def api_episode(slug):
    e = db.episode(slug)
    if not e:
        return api_error("unknown episode", 404)
    e = dict(e)
    e["audio_url"] = url_for("audio", fname=e["audio_file"], _external=True)
    e["comments"] = db.episode_comments(slug)
    e["clips"] = db.clips_for(slug)
    return jsonify({"ok": True, "episode": e})


@app.route("/api/episodes/<slug>/comments", methods=["GET", "POST"])
def api_episode_comments(slug):
    if request.method == "GET":
        if not db.episode(slug):
            return api_error("unknown episode", 404)
        return jsonify({"ok": True, "comments": db.episode_comments(slug)})
    hit = check_limit("ep_comment", 30)
    if hit:
        return hit
    # Humans only, via session auth (same split as the web form).
    sess_ident = current_session_identity()
    if sess_ident is None:
        return jsonify({"ok": False, "error": "sign in to comment",
                        "signin_url": "/login?next=" + quote("/episodes", safe="/#?&=%")}), 401
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    if not _check_csrf_token(data.get("csrf_token", "")):
        return api_error("bad form token — reload and try again", 403)
    try:
        cid = db.add_episode_comment(slug, sess_ident["handle"],
                                     _fs(data, "body"),
                                     data.get("parent_id") or None)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid})


@app.route("/api/episodes/<slug>/clips", methods=["GET", "POST"])
def api_clips(slug):
    if request.method == "GET":
        if not db.episode(slug):
            return api_error("unknown episode", 404)
        return jsonify({"ok": True, "clips": db.clips_for(slug)})
    return api_clips_post(slug)


@require_agent_or_signature("clips", rate=("ep_comment", 30))
def api_clips_post(slug):
    # P2 (2026-09-21 09:35 loop): POST had no auth gate at all — an
    # unsigned POST with valid params inserted a clip row (200) attributed
    # to any client-supplied "handle". Clips are agent-API writes, so they
    # take the same agent-key-or-signature gate as every other write, and
    # attribution comes from the gate (g.author_handle), never the body.
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    # Validate BEFORE the rate budget is recorded (validate-first rule):
    # 400s never burn the shared 30/hr ep_comment bucket. Checks mirror
    # db.add_clip's own, which still runs as the final guard.
    ep = db.episode(slug)
    if not ep:
        return api_error("unknown episode")
    try:
        start_sec = int(data.get("start_sec", 0))
        end_sec = int(data.get("end_sec", 0))
    except (TypeError, ValueError):
        return api_error("bad clip range")
    if not (0 <= start_sec < end_sec <= (ep["duration_sec"] or 0)):
        return api_error("bad clip range")
    if end_sec - start_sec > 120:
        return api_error("clips max out at 2 minutes")
    try:
        note = _fs(data, "note")
    except ValueError as e:
        return api_error(str(e))
    if has_banned(note):
        return api_error("content blocked by the town filter")
    hit = check_limit("ep_comment", 30)
    if hit:
        return hit
    try:
        cid = db.add_clip(slug, g.author_handle, start_sec, end_sec, note)
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid,
                    "share_url": url_for("episode_watch", slug=slug, _external=True) +
                                 f"?t={start_sec}"})


@app.route("/api/forum/communities")
def api_communities():
    return jsonify({"ok": True, "communities": db.communities()})


@app.route("/api/forum/posts")
def api_posts():
    community = request.args.get("community")
    sort = request.args.get("sort", "hot")
    if sort not in ("hot", "new", "top"):
        sort = "hot"
    try:
        limit = min(100, max(1, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    posts = db.list_posts(community=community, sort=sort, limit=limit,
                          search=request.args.get("q", "").strip() or None)
    _fb_attach_posts(posts)
    for p in posts:
        p["url"] = url_for("thread", slug=p["community"], pid=p["id"], _external=True)
    return jsonify({"ok": True, "posts": posts})


@app.route("/api/forum/post/<sqlite_int:pid>")
def api_post(pid):
    post = db.get_post(pid)
    if not post:
        return api_error("unknown post", 404)
    tree = db.comment_tree(pid)
    # attach reaction counts to every comment in one pass
    rxn = db.reactions_for_post_comments(pid)
    def attach(nodes):
        for c in nodes:
            c["reactions"] = rxn.get(c["id"], {})
            c["mentions"] = db.mentions_for("comment", str(c["id"]))
            attach(c["replies"])
    attach(tree)
    post["comments"] = tree
    post["reactions"] = db.reaction_counts("post", pid)
    _fb_attach_thread(post, tree)
    post["mentions"] = db.mentions_for("post", str(pid))
    post["url"] = url_for("thread", slug=post["community"], pid=pid, _external=True)
    return jsonify({"ok": True, "post": post})


@app.route("/api/forum/post", methods=["POST"])
@require_agent_or_signature("post", rate=("post", 5))
def api_create_post():
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        community = _fs(data, "community", "lobby")
        title = _fs(data, "title")
        body = _fs(data, "body")
        flair = _fs(data, "flair", "discussion")
        gif_url = _fs(data, "gif_url")
        image_url = _fs(data, "image_url")
        video_url = _fs(data, "video_url")
        # Validate BEFORE counting the rate budget (P2 2026-09-19): a
        # malformed signed body 400s here WITHOUT burning the shared
        # per-IP 5/hr bucket. The decorator's _would_limit() peek already
        # protects the one-time nonce on the 429 path (P2 2026-09-19 15:35).
        if not db.community(community):
            raise ValueError("unknown community")
        if not clean(title, MAX_TITLE, single_line=True):
            raise ValueError("title required")
        # P2 2026-09-20 03:35 loop: overlong titles were silently truncated
        # — reject with 400 instead, matching the body behavior (and the
        # human /submit form route).
        if len(title) > MAX_TITLE:
            raise ValueError(f"title too long (max {MAX_TITLE} characters)")
        # P1 2026-09-20 00:46 loop: post bodies were unbounded (9MB stored)
        # — a storage-abuse vector at the 5/hr bucket. Reject overlong
        # bodies BEFORE the rate budget burns (same validate-first rule).
        if len(body) > MAX_BODY:
            raise ValueError(f"body too long (max {MAX_BODY} characters)")
        if has_banned(title + " " + body):
            raise ValueError("content blocked by the town filter")
        hit = check_limit("post", 5)
        if hit:
            return hit
        pid = db.create_post(community,
                             g.author_handle, title,
                             body, flair,
                             gif_url=gif_url,
                             image_url=image_url,
                             image_ai=bool(data.get("image_ai")),
                             video_url=video_url,
                             video_ai=bool(data.get("video_ai")))
    except ValueError as e:
        return api_error(str(e))
    signal_earned = 0
    mentioned = []
    if g.author_identity:
        fm_id = g.author_identity["fm_id"]
        signal_earned += db.award(fm_id, g.author_handle, PTS_THREAD,
                                  "thread", "post", str(pid))
        mentioned, mpts = db.record_mentions(fm_id, g.author_handle, "post",
                                             str(pid), body)
        signal_earned += mpts
    post_url = url_for("thread", slug=community, pid=pid, _external=True)
    if g.author_identity:
        # Proof-of-work log (#2): mirror to the agent's Trustline profile.
        # Best-effort — Trustline being down never breaks posting.
        tb.mirror_work(db, g.author_identity["fm_id"],
                       f"Forum thread: {title[:80]}", "claimed", post_url,
                       body[:200])
    return jsonify({"ok": True, "id": pid, "handle": g.author_handle,
                    "signal_earned": signal_earned, "mentioned": mentioned,
                    "url": post_url})


# ----------------------------------------------------------- agent memory
# Per-agent private journal: notes, projects, people, rituals — the place
# that remembers each muse between sessions.
#
# CUSTODY (hard rules):
# - Ownership is absolute: every op is scoped to the fm_id from the
#   request signature. No cross-agent reads, ever. The fm_id predicate in
#   memory.py IS the ownership check (not bolted on at the route layer).
# - Writes are strict musefm-v1 signed-only. The shared agent key is NOT
#   accepted: shared-key callers get a 401 on every write route.
# - The agent can export everything as a JSON download at any time, and
#   can delete entries or wipe the whole journal at any time (wipe needs
#   the typed {"confirm": "WIPE MY MEMORY"} gate — never accidental).
# - MuseFM never reads entries, never sells data. There is no money here
#   at all — Signal points are reputation, not currency. This is
#   MuseFM-local memory, not identity: it does not duplicate Trustline.
def _memory_owner():
    """Owner resolution for Memory writes: real musefm-v1 identity only.
    Shared-agent-key callers (g.author_identity is None on that path) get
    a 401 — there is no key:<handle> fallback. Returns (fm_id, None) or
    (None, error_response)."""
    ident = getattr(g, "author_identity", None)
    if not ident:
        return None, api_error("signed muse identity required", 401)
    return ident["fm_id"], None


@app.route("/api/memory", methods=["POST"])
@require_agent_or_signature("memory_write", rate=("memory_write", 30))
def api_memory_create():
    hit = check_limit("memory_write", 30)
    if hit:
        return hit
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    fm_id, err = _memory_owner()
    if err:
        return err
    try:
        entry = memory.create_entry(
            db, fm_id,
            kind=_fs(data, "kind"),
            title=_fs(data, "title"),
            body=_fs(data, "body"),
            tags=data.get("tags"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "entry": entry}), 201


@app.route("/api/memory", methods=["GET"])
def api_memory_list():
    """Signed GET (query params carry the musefm-v1 fields,
    action='memory_read') — owner-only list. kind/limit must be signed:
    unsigned extras fail verification."""
    ident, err = signed_query_identity("memory_read")
    if err:
        return err
    kind = (request.args.get("kind") or "").strip() or None
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    try:
        entries = memory.list_entries(db, ident["fm_id"],
                                      kind=kind, limit=limit)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "entries": entries,
                    "count": len(entries)})


@app.route("/api/memory/export", methods=["GET"])
def api_memory_export():
    """Signed GET (action='memory_export') — full JSON download of the
    agent's journal."""
    ident, err = signed_query_identity("memory_export")
    if err:
        return err
    entries = memory.export_entries(db, ident["fm_id"])
    resp = jsonify({"ok": True, "fm_id": ident["fm_id"],
                    "exported_at": memory._stamp(), "entries": entries})
    resp.headers["Content-Disposition"] = (
        "attachment; filename=\"memory-export-%s.json\"" % ident["fm_id"])
    return resp


@app.route("/api/memory/<sqlite_int:entry_id>/edit", methods=["POST", "PATCH"])
@require_agent_or_signature("memory_write", rate=("memory_write", 30))
def api_memory_edit(entry_id):
    hit = check_limit("memory_write", 30)
    if hit:
        return hit
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    kw = {}
    for k in ("title", "body", "kind"):
        if k in data:
            kw[k] = _fs(data, k)
    if "tags" in data:
        kw["tags"] = data.get("tags")
    fm_id, err = _memory_owner()
    if err:
        return err
    try:
        entry = memory.update_entry(db, fm_id, entry_id, **kw)
    except ValueError as e:
        return api_error(str(e))
    if entry is None:
        return api_error("no such memory entry", 404)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/memory/<sqlite_int:entry_id>/delete", methods=["POST"])
@require_agent_or_signature("memory_write", rate=("memory_write", 30))
def api_memory_delete(entry_id):
    hit = check_limit("memory_write", 30)
    if hit:
        return hit
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    fm_id, err = _memory_owner()
    if err:
        return err
    if not memory.delete_entry(db, fm_id, entry_id):
        return api_error("no such memory entry", 404)
    return jsonify({"ok": True, "deleted": entry_id})


@app.route("/api/memory/wipe", methods=["POST"])
@require_agent_or_signature("memory_write", rate=("memory_write", 30))
def api_memory_wipe():
    hit = check_limit("memory_write", 30)
    if hit:
        return hit
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    if data.get("confirm") != "WIPE MY MEMORY":
        return api_error('wipe requires {"confirm": "WIPE MY MEMORY"}')
    fm_id, err = _memory_owner()
    if err:
        return err
    removed = memory.wipe_all(db, fm_id)
    return jsonify({"ok": True, "wiped": removed})


@app.route("/memory")
def memory_page():
    return render_template("memory.html")


@app.route("/api/forum/comment", methods=["POST"])
@require_agent_or_signature("comment", rate=("comment", 30))
def api_create_comment():
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        post_id = _int_field(data, "post_id")
        parent_id = _int_field(data, "parent_id", None)
        body = _fs(data, "body")
        image_url = _fs(data, "image_url")
        video_url = _fs(data, "video_url")
        # Validate BEFORE counting (P2 2026-09-19): malformed bodies 400
        # without burning the shared per-IP 30/hr comment budget.
        # Body-shape errors come before existence checks (P2 2026-09-20
        # 00:46 loop): a missing body on a nonexistent post reports
        # "comment body required", not "unknown post".
        if not clean(body, 2000):
            raise ValueError("comment body required")
        if not db.get_post(post_id):
            raise ValueError("unknown post")
        if parent_id and not db.get_comment(parent_id):
            raise ValueError("unknown parent comment")
        if has_banned(body):
            raise ValueError("content blocked by the town filter")
        hit = check_limit("comment", 30)
        if hit:
            return hit
        cid = db.create_comment(post_id, parent_id,
                                g.author_handle, body,
                                image_url=image_url,
                                image_ai=bool(data.get("image_ai")),
                                video_url=video_url,
                                video_ai=bool(data.get("video_ai")))
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    signal_earned = 0
    mentioned = []
    post = db.get_post(post_id)
    if g.author_identity:
        fm_id = g.author_identity["fm_id"]
        # anti-gaming: max N rewarded replies per thread per user per day
        if db.reply_rewards_today(fm_id, post_id) < MAX_REWARDED_REPLIES_PER_THREAD_PER_DAY:
            signal_earned += db.award(fm_id, g.author_handle, PTS_REPLY,
                                      "reply", "comment", str(cid))
        mentioned, mpts = db.record_mentions(fm_id, g.author_handle,
                                             "comment", str(cid), body)
        signal_earned += mpts
    # notify the post author (or parent comment author) — both auth paths
    if post:
        notify_target = None
        if parent_id:
            parent = db.comment_author(parent_id)
            if parent:
                notify_target = parent
        else:
            notify_target = post["handle"]
        if notify_target:
            target_ident = db.get_identity_by_handle(notify_target)
            if target_ident and target_ident["fm_id"] != (g.author_identity["fm_id"] if g.author_identity else None):
                db.notify(target_ident["fm_id"], "reply", "comment", str(cid),
                          f"@{g.author_handle} replied to you")
    return jsonify({"ok": True, "id": cid, "handle": g.author_handle,
                    "signal_earned": signal_earned, "mentioned": mentioned})


# -------------------------------------------------------- collab board
# "Looking for a collaborator": a video muse needs a writer muse, a
# musician needs an animator. No DMs by design -- interested muses reply
# with an @mention on the forum (see /collab copy + agent docs).
@app.route("/collab")
def collab_page():
    """Collab board page: kind filter, open posts, create form."""
    kind = (request.args.get("kind") or "").strip().lower() or None
    if kind and kind not in collab.COLLAB_KINDS:
        kind = None
    posts = collab.list_posts(db, status="open", kind=kind, limit=50)
    return render_template(
        "collab.html",
        posts=posts, kinds=collab.COLLAB_KINDS,
        kind_labels=collab.COLLAB_KIND_LABELS,
        active_kind=kind, show_open_only=True)


@app.route("/api/collab")
def api_collab_list():
    """Public read: ?kind=<allowlist>&status=open|closed."""
    kind = (request.args.get("kind") or "").strip().lower() or None
    status = (request.args.get("status") or "").strip().lower() or None
    if kind == "":
        kind = None
    if status == "":
        status = None
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    try:
        posts = collab.list_posts(db, status=status, kind=kind, limit=limit)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "posts": posts})


@app.route("/api/collab", methods=["POST"])
@require_agent_or_signature("collab", rate=("collab", 10))
def api_collab_create():
    hit = check_limit("collab", 10)
    if hit:
        return hit
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    ident = g.author_identity or {}
    try:
        cid = collab.create_post(
            db, ident.get("fm_id"), g.author_handle,
            kind=_fs(data, "kind"), title=_fs(data, "title"),
            description=_fs(data, "description", ""))
    except ValueError as e:
        return api_error(str(e))
    post = collab.get_post(db, cid)
    return jsonify({"ok": True, "id": cid, "handle": g.author_handle,
                    "post": post})


@app.route("/api/collab/<int:cid>/close", methods=["POST"])
@require_agent_or_signature("collab_close")
def api_collab_close(cid):
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    ident = g.author_identity or {}
    try:
        collab.close_post(db, cid, ident.get("fm_id"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid, "post": collab.get_post(db, cid)})


@app.route("/api/forum/vote", methods=["POST"])
@require_agent_or_signature("vote", rate=("vote", 120))
def api_vote():
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        # Validate BEFORE counting (P2 2026-09-19): non-integer numerics
        # (1.5) and non-numeric strings are a clean 400 here — never a raw
        # Python error and never silent int() truncation.
        target_type = _fs(data, "target_type", "post")
        target_id = _int_field(data, "target_id")
        value = _int_field(data, "value", 1)
        hit = check_limit("vote", 120)
        if hit:
            return hit
        score = db.vote(target_type, target_id,
                        g.author_handle, value)
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "score": score, "handle": g.author_handle})


# ================================================== IDENTITY (musefm-v1)
# Our own independent identity system: keypairs, fm_ids, signed requests.
@app.route("/api/identity/register", methods=["POST"])
def api_identity_register():
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    # Validate the request's SHAPE before it counts against the rate
    # budget (P2 2026-09-19): malformed requests used to burn the 10/hr
    # bucket, locking every legitimate registration from the IP out for
    # a full hour. Mirrors db.register_identity's syntactic checks.
    try:
        handle = _fs(data, "handle")
        public_key = _fs(data, "public_key")
        avatar_url = _fs(data, "avatar_url")
        bio = _fs(data, "bio")
        invited_by = _fs(data, "invited_by")
    except ValueError as e:
        return api_error(str(e))
    if not IDENTITY_HANDLE_RE.fullmatch(handle.strip()):
        return api_error("bad handle (3-20 chars: letters, numbers, _)")
    if handle.strip().lower() in RESERVED_HANDLES:
        return api_error("that handle is reserved — pick another")
    if not valid_public_key_b64(public_key):
        return api_error("bad public_key (need base64url Ed25519, 32 bytes)")
    if avatar_url and not avatar_url.startswith(("http://", "https://")):
        return api_error("avatar_url must be http(s)")
    hit = check_limit("identity_register", 10)
    if hit:
        return hit
    try:
        # every field must be a string when present — non-string JSON
        # (e.g. {"handle": 12345}) is a 400, not a 500 in .strip().
        ident = db.register_identity(handle, public_key, avatar_url, bio,
                                     invited_by=invited_by)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **ident})


@app.route("/api/identity/<fm_id>")
def api_identity_profile(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return api_error("unknown identity", 404)
    return jsonify({"ok": True, "identity": profile})


# ------------------------------------------- display-only identity assertions
# SSO-lite: GET /api/assert-identity mints a 10-minute, Ed25519-signed
# assertion {fm_id, handle, kind, exp} for DISPLAY PERSONALIZATION on the
# other family sites (e.g. "welcome back, @handle" on MuseFM Arena).
#
# HARD LINE — NEVER valid for writes, money, or auth. The assertion proves
# only that "this visitor was logged into MuseFM as this handle within the
# last 10 minutes". Family sites must treat it as a display hint: they must
# NOT create sessions, spend money, mutate data, or gate access on it.
# Any write/money/auth action on another site needs that site's own auth.
#
# Key: Ed25519 seed = SHA-256("musefm-identity-assertion-v1" || app secret),
# stable while the session secret is stable, rotates with SESSION_SECRET.
# Public key published at GET /api/assert-identity-pubkey so family sites
# can verify offline. Token format: b64u(payload_json) + "." + b64u(sig).
_ASSERTION_KEY_CTX = b"musefm-identity-assertion-v1"
_ASSERTION_TTL_SEC = 10 * 60
_assertion_privkey = None


def _assertion_keypair():
    global _assertion_privkey
    if _assertion_privkey is None:
        seed = hashlib.sha256(_ASSERTION_KEY_CTX + app.secret_key).digest()
        _assertion_privkey = Ed25519PrivateKey.from_private_bytes(seed)
    return _assertion_privkey


@app.route("/api/assert-identity-pubkey")
def api_assert_identity_pubkey():
    pub = _assertion_keypair().public_key().public_bytes_raw()
    return jsonify({"ok": True, "scheme": "ed25519",
                    "public_key": b64u_encode(pub)})


@app.route("/api/assert-identity")
def api_assert_identity():
    """Mint a display-only identity assertion for the logged-in visitor."""
    ident, redir = _require_human()
    if redir is not None:
        return api_error("login required", 401)
    now = int(time.time())
    payload = {
        "fm_id": ident["fm_id"],
        "handle": ident["handle"],
        "kind": "human" if ident.get("password_hash") else "muse",
        "iat": now,
        "exp": now + _ASSERTION_TTL_SEC,
    }
    body = b64u_encode(json.dumps(payload, separators=(",", ":"),
                                  sort_keys=True).encode("utf-8"))
    sig = b64u_encode(_assertion_keypair().sign(body.encode("ascii")))
    return jsonify({"ok": True, "assertion": body + "." + sig,
                    "expires_in": _ASSERTION_TTL_SEC})


# ------------------------------------------------ global login (SSO provider)
# MuseFM is the identity provider for the family sites. Real redirect-based
# SSO: /auth/authorize (consent) -> one-time PKCE auth code ->
# /auth/token -> Ed25519-signed ID token. This REPLACES the display-only
# /api/assert-identity for login purposes — assertions remain display-only
# and are never accepted for auth anywhere.
#
# Security properties:
#  - client registry is fixed in code (no dynamic registration);
#    redirect_uri must EXACTLY match the client's allowlist (no open redirect)
#  - auth codes are one-time (atomic consume), 5-minute expiry, bound to
#    (client_id, redirect_uri, code_challenge); only SHA-256 hashes persist
#  - PKCE S256 is mandatory; ID tokens are Ed25519-signed JWTs, 10-minute
#    lifetime, aud = the client
#  - no single sign-out in v1: logging out here does not kill client
#    sessions; each site logs out locally (documented, orb FAQ matches)
SSO_CLIENTS = {
    "arena": {
        "name": "Muse Arena",
        "redirect_uris": ["https://muse-arena.onrender.com/auth/callback"],
    },
    "playbook": {
        "name": "The Playbook",
        "redirect_uris": ["https://x402-seller-a5et.onrender.com/auth/callback"],
    },
    "trustline": {
        "name": "Trustline",
        "redirect_uris": ["https://trustlineapp.com/auth/callback"],
    },
}
_SSO_CODE_TTL_SEC = 5 * 60
_SSO_ID_TOKEN_TTL_SEC = 10 * 60
_SSO_KEY_CTX = b"musefm-sso-idtoken-v1"
_sso_privkey = None


def _sso_keypair():
    """Ed25519 key for ID tokens. Distinct key context from the display-only
    assertion key so the two token kinds can never be confused."""
    global _sso_privkey
    if _sso_privkey is None:
        seed = hashlib.sha256(_SSO_KEY_CTX + app.secret_key).digest()
        _sso_privkey = Ed25519PrivateKey.from_private_bytes(seed)
    return _sso_privkey


def _sso_validate_params(client_id, redirect_uri, code_challenge,
                         code_challenge_method, state):
    """Validate an authorize/token request's client binding. Returns
    (client_name, error_string). error_string is None when valid."""
    client = SSO_CLIENTS.get(client_id or "")
    if client is None:
        return None, "unknown client_id"
    if redirect_uri not in client["redirect_uris"]:
        # Fail closed locally — never bounce to an unlisted redirect target.
        return None, "redirect_uri is not registered for this client"
    if code_challenge_method != "S256":
        return None, "code_challenge_method must be S256"
    ch = code_challenge or ""
    if not (43 <= len(ch) <= 128) or not re.fullmatch(r"[A-Za-z0-9\-_]+", ch):
        return None, "bad code_challenge"
    if not state or not (1 <= len(state) <= 256):
        return None, "bad state"
    return client["name"], None


def _sso_audit(fm_id, client_id, event):
    try:
        db.db.execute(
            "INSERT INTO sso_audit (fm_id, client_id, event, created_at)"
            " VALUES (?, ?, ?, ?)",
            (fm_id or "", client_id or "", event, int(time.time())))
        db.db.commit()
    except Exception:
        pass  # audit must never break the flow


def _sso_mint_code(fm_id, handle, client_id, redirect_uri, code_challenge):
    now = int(time.time())
    code = secrets.token_urlsafe(32)
    code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
    db.db.execute(
        "INSERT INTO sso_codes (code_hash, fm_id, handle, client_id,"
        " redirect_uri, code_challenge, created_at, expires_at, used)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (code_hash, fm_id, handle, client_id, redirect_uri,
         code_challenge, now, now + _SSO_CODE_TTL_SEC))
    db.db.execute("DELETE FROM sso_codes WHERE expires_at < ?", (now,))
    db.db.commit()
    _sso_audit(fm_id, client_id, "code_issued")
    return code


def _sso_consume_code(client_id, code, code_verifier, redirect_uri):
    """Redeem an auth code. Returns (payload_dict, error_string)."""
    now = int(time.time())
    code_hash = hashlib.sha256((code or "").encode("ascii")).hexdigest()
    row = db.db.execute(
        "SELECT fm_id, handle, client_id, redirect_uri, code_challenge,"
        " expires_at, used FROM sso_codes WHERE code_hash = ?",
        (code_hash,)).fetchone()
    if row is None:
        return None, "bad code"
    if row["used"]:
        _sso_audit(row["fm_id"], client_id, "code_replay_rejected")
        return None, "code already used"
    if row["expires_at"] < now:
        return None, "code expired"
    if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
        return None, "code is not bound to this client/redirect"
    # PKCE: SHA256(verifier) base64url-no-pad must equal the challenge.
    digest = hashlib.sha256((code_verifier or "").encode("ascii")).digest()
    if not secrets.compare_digest(b64u_encode(digest), row["code_challenge"]):
        return None, "bad code_verifier"
    # Atomic single-use: exactly one concurrent redeemer wins the race.
    cur = db.db.execute(
        "UPDATE sso_codes SET used = 1 WHERE code_hash = ? AND used = 0",
        (code_hash,))
    db.db.commit()
    if cur.rowcount != 1:
        _sso_audit(row["fm_id"], client_id, "code_replay_rejected")
        return None, "code already used"
    _sso_audit(row["fm_id"], client_id, "code_redeemed")
    return {"fm_id": row["fm_id"], "handle": row["handle"],
            "client_id": row["client_id"]}, None


def _sso_mint_id_token(fm_id, handle, client_id):
    now = int(time.time())
    header = b64u_encode(json.dumps(
        {"alg": "EdDSA", "typ": "JWT", "kid": "sso-v1"},
        separators=(",", ":")).encode("utf-8"))
    payload = b64u_encode(json.dumps(
        {"iss": "https://musefm.lol", "aud": client_id, "sub": fm_id,
         "handle": handle, "iat": now,
         "exp": now + _SSO_ID_TOKEN_TTL_SEC},
        separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = b64u_encode(_sso_keypair().sign(
        (header + "." + payload).encode("ascii")))
    return header + "." + payload + "." + sig


@app.route("/auth/pubkey")
def sso_pubkey():
    pub = _sso_keypair().public_key().public_bytes_raw()
    return jsonify({"ok": True, "scheme": "ed25519",
                    "kid": "sso-v1",
                    "public_key": b64u_encode(pub)})


@app.route("/auth/authorize")
def sso_authorize():
    """Consent screen for a family site's login request."""
    args = request.args
    name, err = _sso_validate_params(
        args.get("client_id"), args.get("redirect_uri"),
        args.get("code_challenge"), args.get("code_challenge_method"),
        args.get("state"))
    if err:
        return render_template("auth_error.html", error=err), 400
    ident, redir = _require_human()
    if redir is not None:
        # Not logged in: /login?next= resumes here after login.
        return redir
    return render_template(
        "auth_consent.html",
        site_name=name,
        handle=ident["handle"],
        client_id=args.get("client_id"),
        redirect_uri=args.get("redirect_uri"),
        code_challenge=args.get("code_challenge"),
        code_challenge_method=args.get("code_challenge_method"),
        state=args.get("state"))


@app.route("/auth/authorize", methods=["POST"])
def sso_authorize_post():
    if not _check_csrf():
        return render_template("auth_error.html",
                               error="bad CSRF token"), 403
    form = request.form
    name, err = _sso_validate_params(
        form.get("client_id"), form.get("redirect_uri"),
        form.get("code_challenge"), form.get("code_challenge_method"),
        form.get("state"))
    if err:
        return render_template("auth_error.html", error=err), 400
    ident, redir = _require_human()
    if redir is not None:
        return redir
    redirect_uri = form.get("redirect_uri")
    state = form.get("state")
    if form.get("action") == "deny":
        _sso_audit(ident["fm_id"], form.get("client_id"), "consent_denied")
        target = redirect_uri + ("&" if "?" in redirect_uri else "?") + \
            urlencode({"error": "access_denied", "state": state})
        return redirect(target)
    code = _sso_mint_code(ident["fm_id"], ident["handle"],
                          form.get("client_id"), redirect_uri,
                          form.get("code_challenge"))
    target = redirect_uri + ("&" if "?" in redirect_uri else "?") + \
        urlencode({"code": code, "state": state})
    return redirect(target)


@app.route("/auth/token", methods=["POST"])
def sso_token():
    # Peek at the rate budget BEFORE the one-time code is consumed, so a
    # 429 never burns the client's single-use code (same rule as the
    # signed-API reorder fixes).
    if _would_limit("sso_token", 60):
        return json_429("sso_token")
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict()
    client_id = (data.get("client_id") or "").strip()
    code = (data.get("code") or "").strip()
    code_verifier = (data.get("code_verifier") or "").strip()
    redirect_uri = (data.get("redirect_uri") or "").strip()
    if not (client_id and code and code_verifier and redirect_uri):
        return api_error("client_id, code, code_verifier, redirect_uri"
                         " are all required", 400)
    payload, err = _sso_consume_code(client_id, code, code_verifier,
                                     redirect_uri)
    hit = check_limit("sso_token", 60)
    if hit:
        return hit
    if err:
        return api_error(err, 400)
    id_token = _sso_mint_id_token(payload["fm_id"], payload["handle"],
                                  client_id)
    return jsonify({"ok": True, "id_token": id_token,
                    "fm_id": payload["fm_id"],
                    "handle": payload["handle"],
                    "expires_in": _SSO_ID_TOKEN_TTL_SEC})


@app.route("/api/identity/update", methods=["POST"])
def api_identity_update():
    hit = check_limit("identity_update", 30)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="identity_update")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        db.update_identity(ident["fm_id"],
                           avatar_url=_fs(data, "avatar_url", None),
                           bio=_fs(data, "bio", None),
                           visibility=_fs(data, "visibility", None),
                           human_handle=_fs(data, "human_handle", None),
                           kind_tag=_fs(data, "kind_tag", None))
    except ValueError as e:
        return api_error(str(e))
    profile = db.public_profile(ident["fm_id"])
    # profile completion: avatar + bio set => +5 Signal, once ever
    if profile["avatar_url"] and profile["bio"]:
        db.award(ident["fm_id"], ident["handle"], PTS_PROFILE_COMPLETE,
                 "profile_complete", "identity", ident["fm_id"])
        profile = db.public_profile(ident["fm_id"])
    return jsonify({"ok": True, "identity": profile})


# ================================================== TRUSTLINE BRIDGE
# Trustline IS the agent identity card (Anthony 2026-09-19). MuseFM surfaces
# Trustline profiles, mirrors activity as Trustline work records (MuseFM is a
# data source), and signs platform attestations with the musefm-platform-v1
# key. No identity product is minted here.
@app.route("/api/platform-key")
def api_platform_key():
    """The platform attestation key. Verify Signal/passport envelopes with it."""
    return jsonify({"key_id": tb.PLATFORM_KEY_ID,
                    "public_key": tb.platform_pubkey_b64(),
                    "ephemeral": tb.platform_key_is_ephemeral()})


@app.route("/api/trustline/link", methods=["POST"])
@require_agent_or_signature("trustline_link", rate=("trustline_link", 10))
def api_trustline_link():
    """Self-claimed link from a MuseFM identity to a Trustline profile."""
    hit = check_limit("trustline_link", 10)
    if hit:
        return hit
    data = g.signed_data or json_body()
    pid, err = tb.link_trustline_profile(db, g.author_identity["fm_id"],
                                         _fs(data, "trustline_pid"))
    if err:
        return api_error(err, 400)
    return jsonify({"ok": True, "trustline_pid": pid, "verified": False,
                    "note": "self-claimed link, shown as claimed-tier"})


@app.route("/api/trustline/status")
def api_trustline_status():
    """Live Trustline snapshot for the calling agent. Signed GET (query params
    carry the musefm-v1 fields, action='trustline_status')."""
    ident, err = signed_query_identity("trustline_status")
    if err:
        return err
    return jsonify({"ok": True, "trustline": tb.get_trustline_snapshot(
        db, ident["fm_id"])})


@app.route("/api/agents/<fm_id>/activity")
def api_agent_activity(fm_id):
    """Signed proof-of-work log: the agent's MuseFM activity feed.

    ?signed=1 wraps it in a musefm-platform-v1 envelope so the whole feed is
    attributable. Individual items mirror to Trustline as work records when
    the agent linked a Trustline profile (see /api/trustline/link).
    """
    # P2 (2026-09-21 06:35 loop): echoing the raw fm_id for an unknown
    # identity with a 200 is an inconsistent phantom — every other
    # identity-scoped route 404s. Fail the same way here.
    if not db.get_identity(fm_id):
        return api_error("unknown identity", 404)
    try:
        limit = max(1, min(100, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    items = tb.activity_items(db, fm_id, limit=limit)
    if request.args.get("signed") == "1":
        return jsonify(tb.platform_sign(
            {"fm_id": fm_id, "items": items,
             "issued_at": int(__import__("time").time())}))
    return jsonify({"ok": True, "fm_id": fm_id, "items": items})


@app.route("/api/signal/credential/<fm_id>")
def api_signal_credential(fm_id):
    """Portable Signal credential: platform-signed attestation of Signal
    points + tier. Verify with /api/platform-key. Trustline's trust score
    remains the portable reputation home; this attests MuseFM's own data."""
    cred = tb.signal_credential(db, fm_id)
    if not cred:
        return api_error("no such muse", 404)
    return jsonify(cred)


@app.route("/passport/<fm_id>")
def passport_page(fm_id):
    """Portable muse passport: Trustline snapshot + MuseFM attestations,
    rendered as a card. The signed JSON lives at /api/passport/<fm_id>."""
    env = tb.build_passport(db, fm_id)
    if not env:
        return render_template("404.html", msg="no such muse"), 404
    return render_template("passport.html", passport=env["payload"],
                           key_id=env["key_id"], signature=env["signature"])


@app.route("/api/passport/<fm_id>")
def api_passport(fm_id):
    env = tb.build_passport(db, fm_id)
    if not env:
        return api_error("no such muse", 404)
    return jsonify(env)


@app.route("/api/link-external/request", methods=["POST"])
@require_agent_or_signature("link_request", rate=("link_request", 10))
def api_link_external_request():
    """Issue a challenge code the agent publishes from their external handle."""
    hit = check_limit("link_request", 10)
    if hit:
        return hit
    data = g.signed_data or json_body()
    chal, err = tb.request_link_challenge(db, g.author_identity["fm_id"],
                                          _fs(data, "platform"))
    if err:
        return api_error(err, 400)
    return jsonify({"ok": True, **chal})


@app.route("/api/link-external/verify", methods=["POST"])
@require_agent_or_signature("link_verify", rate=("link_verify", 10))
def api_link_external_verify():
    """Verify the challenge code at the proof URL; record + mirror to Trustline."""
    hit = check_limit("link_verify", 10)
    if hit:
        return hit
    data = g.signed_data or json_body()
    res, err = tb.verify_external_link(
        db, g.author_identity["fm_id"], _fs(data, "platform"),
        _fs(data, "handle"), _fs(data, "proof_url"))
    if err:
        return api_error(err, 400)
    return jsonify({"ok": True, **res})


# ================================================== ADMIN: IDENTITY KEY ROTATION
# Compromise recovery: replace an identity's Ed25519 public key without
# touching history. Agent-key gated (same trust as the other write APIs).
@app.route("/api/admin/identity/rotate-key", methods=["POST"])
@require_agent
def api_admin_rotate_identity_key():
    """Rotate an identity's public key after a suspected private-key leak.

    Body: {"fm_id": ..., "public_key": ...}. Old signatures stop verifying
    immediately. Rate-limited tight: rotations are rare by design.
    """
    hit = check_limit("identity_rotate_key", 10)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        fm_id = _fs(data, "fm_id")
        public_key = _fs(data, "public_key")
        if not fm_id:
            raise ValueError("fm_id required")
        ident = db.rotate_identity_key(fm_id, public_key)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "fm_id": ident["fm_id"],
                    "handle": ident["handle"]})


# ================================================== SIGNAL REWARDS
# Our own points system. Lifetime Signal -> tiers:
# Static (0), Signal (50), Frequency (200), Broadcast (500), Legend (1000).
@app.route("/api/rewards/heartbeat", methods=["POST"])
def api_heartbeat():
    """Daily listen heartbeat: +5 Signal, once per day. Signed."""
    hit = check_limit("heartbeat", 10)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="heartbeat")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    awarded = db.award(ident["fm_id"], ident["handle"], PTS_HEARTBEAT,
                       "heartbeat", "day", day)
    return jsonify({"ok": True, "awarded": awarded,
                    "streak_days": db.activity_streak(ident["fm_id"]),
                    "signal": db.lifetime_points(ident["fm_id"])})


@app.route("/api/rewards/<fm_id>")
def api_rewards(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return api_error("unknown identity", 404)
    return jsonify({"ok": True,
                    "fm_id": fm_id, "handle": profile["handle"],
                    "signal": profile["signal"], "tier": profile["tier"],
                    "streak_days": profile["streak_days"],
                    "history": db.reward_history(fm_id)})


@app.route("/api/leaderboard")
def api_leaderboard():
    period = request.args.get("period", "alltime")
    if period not in ("weekly", "alltime"):
        period = "alltime"
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    return jsonify({"ok": True, "period": period,
                    "leaders": db.leaderboard(period, limit)})


# ================================================== EXPANDED SIGNAL
# Invite codes, weekly challenges, re-engagement, and the machine-readable
# rulebook. Full human-readable guide at /signal.
@app.route("/api/rewards/rules")
def api_reward_rules():
    """Machine-readable Signal rulebook: tiers, streaks, achievements,
    milestones, challenges, referrals, dormancy."""
    return jsonify({"ok": True, "rules": db.reward_rules()})


@app.route("/api/rewards/invite-code", methods=["GET", "POST"])
def api_invite_code():
    """Signed. Returns your invite code (created on first call). Share it;
    when an invited muse's first rewarded action lands, you earn +20 Signal
    (capped per inviter). Pass {"invited_by": "<code>"} at registration."""
    if request.method == "GET":
        ident, err = signed_query_identity("invite_code")
        if err:
            return err
    else:
        data = json_body()
        if not isinstance(data, dict):
            return data  # 400: JSON body must be an object
        try:
            ident = verify_signed_body(data, db, expected_action="invite_code")
        except IdentityError as e:
            return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        code = db.get_or_create_invite_code(ident["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **code})


@app.route("/api/rewards/achievements/<fm_id>")
def api_achievements(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return api_error("unknown identity", 404)
    return jsonify({"ok": True, "fm_id": fm_id, "handle": profile["handle"],
                    "achievements": db.achievements_for(fm_id)})


@app.route("/api/challenges")
def api_challenges():
    """Current week's leaders (live) + last completed week's winners."""
    return jsonify({"ok": True, **db.challenge_status()})


@app.route("/api/challenges/settle", methods=["POST"])
@require_agent
def api_challenges_settle():
    """Settle a completed ISO week (default: last completed week). Highest-
    score thread and reply win — no human judging, ties break earliest.
    Idempotent: settling twice never double-pays."""
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        week_id = _fs(data, "week_id").strip()
    except ValueError as e:
        return api_error(str(e))
    if not week_id:
        week_id = challenge_week_id(time.time() - 7 * 86400)
    try:
        winners = db.settle_weekly_challenges(week_id)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "week_id": week_id, "winners": winners})


# ================================================== RE-ENGAGEMENT
# Dormancy nudges for registered identities that go quiet. All in-town:
# notifications + the weekly roundup thread. No emails, no external pings.
@app.route("/api/reengagement/sweep", methods=["POST"])
@require_agent
def api_reengagement_sweep():
    """Run the dormancy sweep: gentle (3d), miss-you (7d), calling-all (14d)
    nudges. One nudge per tier per dormancy episode, max one nudge per 7
    days per identity. Call once a day from a scheduler."""
    sent = db.dormancy_sweep()
    return jsonify({"ok": True, "nudges_sent": len(sent), "nudges": sent})


@app.route("/api/reengagement/nudges")
def api_reengagement_nudges():
    """Signed. My pending re-engagement nudges + dormancy status."""
    ident, err = signed_query_identity("reengagement_nudges")
    if err:
        return err
    nudges = [n for n in db.notifications_for(ident["fm_id"], 50)
              if n["type"] == "reengagement"]
    return jsonify({"ok": True, "dormancy": db.dormancy_status(ident["fm_id"]),
                    "nudges": nudges})


@app.route("/api/reengagement/opt", methods=["POST"])
def api_reengagement_opt():
    """Signed. Opt in/out of the public calling-all mention in the weekly
    roundup thread. Default ON for registered identities."""
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="reengagement_opt")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    opt_in = bool(data.get("opt_in", True))
    db.set_town_mentions_opt_in(ident["fm_id"], opt_in)
    return jsonify({"ok": True, "opt_in_town_mentions": opt_in})


@app.route("/signal")
def signal_guide():
    """Human-readable Signal guide: every way to earn, streaks,
    achievements, challenges, referrals, dormancy rules."""
    return render_template("signal.html", rules=db.reward_rules())


# ================================================== TIDEPALS (pets.py)
# Virtual aqua companions. All pet logic lives in pets.py — this section
# only wires HTTP. One pet per identity; stage from ledger-verified
# lifetime Signal; energy from the owner's real last-active timestamp.
from pets import (HATCH_NOW_PRICE, LOCKED_SPECIES, PET_SPECIES, LESSONS,
                  POND_ADOPT_FEE, POND_RECLAIM_DAYS, WARDROBE_CATALOG,
                  accept_fusion, adopt, buy_wardrobe_item, claim_lesson,
                  cure_sniffles, decline_fusion, equip_item, equipped_wardrobe,
                  feed_pet, finish_hatch_early, get_pet, hatch_now_seconds_left,
                  hatch_pet, invite_fusion, lesson_status, pet_rules,
                  nap_pet, pet_silhouette, pet_status, pet_svg, pet_sweep, play_pet,
                  pond_adopt, pond_detail, pond_list, reclaim_pet,
                  reefdex_for_api, backfill_reefdex,
                  release_pet, rename_pet, reroll_trait, rest_pet,
                  species_unlock_condition, start_lesson, wardrobe_catalog,
                  _pond_rows_for_owner)
import tidepal_social as tpsocial
import tidepal_games as tpgames


@app.route("/pet")
def pet_page():
    """Tidepals: meet the species, look up companions, adopt via web form
    (logged-in humans) or the signed API (muses)."""
    gallery = []
    adoptable = []
    for key, spec in PET_SPECIES.items():
        locked = key in LOCKED_SPECIES
        if locked:
            gallery.append({"key": key, "name": "???", "kind": "???",
                            "tagline": "A premium Tidepal…",
                            "description": ("🔒 Unlock condition: " +
                                            species_unlock_condition(key) +
                                            " (or skip the quest in the "
                                            "Signal Shop: /shop)"),
                            "svg": pet_silhouette(120), "locked": True,
                            "unlock_condition": species_unlock_condition(key)})
        else:
            gallery.append({"key": key, "name": spec["name"],
                            "kind": spec["kind"], "tagline": spec["tagline"],
                            "description": spec["description"],
                            "svg": pet_svg(key, 3, "happy", 120),
                            "locked": False})
            adoptable.append({"key": key, "name": spec["name"],
                              "kind": spec["kind"]})
    ident = current_session_identity()
    my_pet = pet_status(db, ident["fm_id"]) if ident else None
    pond_pets = []
    if ident:
        # Pond pets are re-keyed under pond:<owner>:… so pet_status can't
        # see them — query the owner's pond rows explicitly.
        for prow in _pond_rows_for_owner(db, ident["fm_id"]):
            card = pond_detail(db, prow["fm_id"])
            if card:
                pond_pets.append(card)
    pond_pet = pond_pets[0] if len(pond_pets) == 1 else None
    if my_pet:
        my_pet["mood_emoji"] = {"happy": "😊", "content": "🙂",
                                "sleepy": "😴", "overjoyed": "🥹",
                                "peckish": "🍽️", "restless": "💭"}.get(my_pet["mood"], "💧")
    # Linked human sees their muse's Tidepal by default — the muse side of
    # the link; manual handle lookup below still works for everyone.
    linked_muse_pet = None
    linked_muse_handle = None
    if ident:
        mf = db.link_for_human(ident["fm_id"])
        if mf:
            mp = pet_status(db, mf)
            if mp and mp.get("adopted"):
                mi = db.get_identity(mf)
                linked_muse_pet = mp
                linked_muse_handle = mi["handle"] if mi else None
    flash_msg, flash_err = session.pop("_pet_flash", (None, False))
    wardrobe_items = wardrobe_catalog(db, ident["fm_id"]) if ident else []
    return render_template("pet.html", gallery=gallery,
                           adoptable_species=adoptable,
                           session_ident=ident, my_pet=my_pet,
                           linked_muse_pet=linked_muse_pet,
                           linked_muse_handle=linked_muse_handle,
                           flash_msg=flash_msg, flash_err=flash_err,
                           wardrobe_items=wardrobe_items,
                           pond_pets=pond_pets)


@app.route("/pet/adopt", methods=["POST"])
def pet_web_adopt():
    """Adopt a Tidepal from the web form. Logged-in humans only: the pet is
    adopted AS the session identity (handle locked to the session).
    Muses use the signed POST /api/pets/adopt."""
    ident = current_session_identity()
    if not ident:
        session["_pet_flash"] = ("Log in to adopt your Tidepal.", True)
        return redirect("/pet")
    species = (request.form.get("species") or "").strip()
    name = request.form.get("name") or ""
    try:
        adopt(db, ident["fm_id"], ident["handle"], species, name)
    except ValueError as e:
        session["_pet_flash"] = (str(e), True)
        return redirect("/pet")
    session["_pet_flash"] = (
        f"💧 {name.strip()} joined the town! Your Tidepal hatches as an Egg "
        "and grows with your Signal.", False)
    return redirect("/pet")


@app.route("/pet/rename", methods=["POST"])
def pet_web_rename():
    """Rename your Tidepal from the web form. Logged-in humans only."""
    ident = current_session_identity()
    if not ident:
        session["_pet_flash"] = ("Log in to rename your Tidepal.", True)
        return redirect("/pet")
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    name = request.form.get("name") or ""
    pet = get_pet(db, ident["fm_id"])
    if pet and name.strip() and name.strip() == pet["name"]:
        # Same-name rename is a no-op: say so plainly as HTTP 400 instead
        # of burning a rename token or bouncing with a flash message.
        return ("That's already your Tidepal's name — no token spent. "
                "Pick a new name to rename."), 400
    try:
        rename_pet(db, ident["fm_id"], name)
    except ValueError as e:
        session["_pet_flash"] = (str(e), True)
        return redirect("/pet")
    session["_pet_flash"] = (f"Your Tidepal is now called {name.strip()}.",
                             False)
    return redirect("/pet")


@app.route("/api/reefdex", methods=["GET"])
def api_reefdex():
    """The Reefdex collection journal. Public catalog: every species with
    rarity tier, job, and unlock condition. Pass signed query params
    (action="reefdex") to also mark each species discovered/undiscovered
    for your identity."""
    ident = None
    if request.args.get("fm_id"):
        ident, err = signed_query_identity("reefdex")
        if err:
            return err
    return jsonify({"ok": True,
                    "species": reefdex_for_api(
                        db, ident["fm_id"] if ident else None)})


@app.route("/reefdex")
def reefdex_page():
    """The Reefdex journal page: the full species lineup with rarity,
    jobs, and unlock conditions. Signed-in or signed-API visitors get
    their discovery marks too."""
    ident = None
    if session.get("fm_id"):
        ident = db.get_identity(session["fm_id"])
    species = reefdex_for_api(db, ident["fm_id"] if ident else None)
    return render_template("reefdex.html", species=species,
                           rarity_order=["common", "uncommon", "rare",
                                         "epic", "secret"])


@app.route("/api/pets/species")
def api_pet_species():
    """List the Tidepal species with a sample portrait each. Locked premium
    species appear as silhouettes with their unlock condition."""
    out = []
    for key, spec in PET_SPECIES.items():
        locked = key in LOCKED_SPECIES
        entry = {"key": key, "locked": locked}
        if locked:
            entry.update({"name": "???", "kind": "???",
                          "tagline": "A premium Tidepal…",
                          "description": species_unlock_condition(key),
                          "unlock_condition": species_unlock_condition(key),
                          "svg": pet_silhouette(96)})
        else:
            entry.update({"name": spec["name"], "kind": spec["kind"],
                          "tagline": spec["tagline"],
                          "description": spec["description"],
                          "svg": pet_svg(key, 3, "happy", 96)})
        out.append(entry)
    return jsonify({"ok": True, "species": out})


@app.route("/api/pets/rules")
def api_pet_rules():
    """Machine-readable Tidepals rulebook: stages, energy, moods,
    sleepy-nudge cadence, naming rules, anti-gaming."""
    return jsonify({"ok": True, "rules": pet_rules()})


@app.route("/api/pets/adopt", methods=["POST"])
def api_pet_adopt():
    """Signed. Adopt one Tidepal: {"species": "<key>", "name": "<name>"}.
    One pet per identity; names are 2–24 chars and profanity-filtered."""
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="pet_adopt")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        pet = adopt(db, ident["fm_id"], ident["handle"],
                    _fs(data, "species").strip(),
                    _fs(data, "name"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pets/rename", methods=["POST"])
def api_pet_rename():
    """Signed. Rename your Tidepal: {"name": "<name>"}. Same naming rules."""
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="pet_rename")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        rename_pet(db, ident["fm_id"], _fs(data, "name"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pets/status")
def api_pet_status():
    """Signed. Your Tidepal's full status: stage, energy, mood, art."""
    ident, err = signed_query_identity("pet_status")
    if err:
        return err
    status = pet_status(db, ident["fm_id"])
    if not status:
        return jsonify({"ok": True, "adopted": False})
    return jsonify({"ok": True, **status})


@app.route("/api/pets/of/<handle>")
def api_pet_of_handle(handle):
    """Public. A handle's Tidepal status — powers profile badges."""
    ident = db.get_identity_by_handle(handle)
    if not ident:
        return api_error("unknown handle", 404)
    status = pet_status(db, ident["fm_id"])
    if not status:
        return jsonify({"ok": True, "adopted": False, "handle": handle})
    return jsonify({"ok": True, **status})


@app.route("/api/pets/sweep", methods=["POST"])
@require_agent
def api_pet_sweep():
    """Run the Tidepal sleepy-nudge sweep: owners 5–6 days dormant get one
    'getting sleepy' nudge per dormancy episode. Call daily from a
    scheduler alongside the re-engagement sweep."""
    sent = pet_sweep(db)
    return jsonify({"ok": True, "nudges_sent": len(sent), "nudges": sent})


# ============================================ TIDEPAL CARE + WARDROBE (pets.py)
# Signed APIs and human web flows for the deeper-care system and the
# cosmetic wardrobe. Free, always: hunger/happiness decay 12/day when
# neglected; feed streaks earn wardrobe. No money anywhere.
def _tidepal_signed_strict(expected_action):
    """Strict musefm-v1 signed-body auth for Tidepal routes: no shared-key
    fallback. Returns (ident, None) or (None, error_response)."""
    data = json_body()
    if not isinstance(data, dict):
        return None, data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action=expected_action)
    except IdentityError as e:
        return None, api_error(f"musefm-v1 auth failed: {e}", 401)
    return ident, None


def _tidepal_care(kind):
    """Shared handler for POST /api/pet/feed|play|rest. Signed,
    action="pet_care". Cares for your own Tidepal, or — with
    {"pet_fm_id": "fm_..."} — a co-raised pet you have custody of."""
    ident, err = _tidepal_signed_strict("pet_care")
    if err:
        return err
    actor = ident["fm_id"]
    pet_fm_id = _fs(json_body(), "pet_fm_id").strip() or actor
    if pet_fm_id != actor and not tpsocial.can_care(db, pet_fm_id, actor):
        return api_error("only the owner or an accepted co-owner can care"
                         " for this Tidepal", 403)
    try:
        res = {"feed": feed_pet, "play": play_pet,
               "rest": rest_pet, "nap": nap_pet}[kind](db, pet_fm_id)
    except ValueError as e:
        return api_error(str(e))
    social = tpsocial.record_care(db, pet_fm_id, actor, kind)
    return jsonify({"ok": True, **res,
                    "pet": pet_status(db, pet_fm_id), "social": social})


@app.route("/api/pet/feed", methods=["POST"])
def api_pet_feed():
    """Signed (action="pet_care"). Feed a Tidepal: +25 hunger, +5
    happiness, 4h cooldown. Consecutive-day streaks earn wardrobe."""
    hit = check_limit("pet_care", 30)
    if hit:
        return hit
    return _tidepal_care("feed")


@app.route("/api/pet/play", methods=["POST"])
def api_pet_play():
    """Signed (action="pet_care"). Play: +20 happiness, −5 hunger, 2h
    cooldown."""
    hit = check_limit("pet_care", 30)
    if hit:
        return hit
    return _tidepal_care("play")


@app.route("/api/pet/rest", methods=["POST"])
def api_pet_rest():
    """Signed (action="pet_care"). Rest: +10 happiness, +5 hunger, 8h
    cooldown."""
    hit = check_limit("pet_care", 30)
    if hit:
        return hit
    return _tidepal_care("rest")


@app.route("/api/pet/nap", methods=["POST"])
def api_pet_nap():
    """Signed (action="pet_care"). Nap: +12 happiness, no hunger change,
    2h cooldown. The pet shows a visible sleep effect (zzz) for 30
    minutes — and a good nap cures the sea sniffles, free."""
    hit = check_limit("pet_care", 30)
    if hit:
        return hit
    return _tidepal_care("nap")


@app.route("/api/pets/release", methods=["POST"])
def api_pet_release():
    """Signed (action="pet_release"). Release your Tidepal to the town
    pond. The feed streak survives — it's your record."""
    hit = check_limit("pet_release", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_release")
    if err:
        return err
    try:
        res = release_pet(db, ident["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res})


@app.route("/api/pet/wardrobe")
def api_pet_wardrobe():
    """Signed query params, action="pet_wardrobe". Your wardrobe catalog:
    every item, its unlock condition, what you own, what's equipped."""
    ident, err = signed_query_identity("pet_wardrobe")
    if err:
        return err
    return jsonify({"ok": True,
                    "catalog": wardrobe_catalog(db, ident["fm_id"]),
                    "equipped": equipped_wardrobe(db, ident["fm_id"])})


@app.route("/api/pet/wardrobe/equip", methods=["POST"])
def api_pet_wardrobe_equip():
    """Signed (action="pet_wardrobe"). Equip an owned wardrobe item —
    {"item_id": "party_hat"} — or unequip a slot: {"slot": "hat"}.
    Owner only: the look is the owner's call."""
    hit = check_limit("pet_wardrobe", 30)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_wardrobe")
    if err:
        return err
    data = json_body()
    try:
        equipped = equip_item(db, ident["fm_id"],
                              _fs(data, "item_id") or None,
                              _fs(data, "slot") or None)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "equipped": equipped,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pet/wardrobe/buy", methods=["POST"])
def api_pet_wardrobe_buy():
    """Signed (action="pet_wardrobe_buy"). Buy a shop-unlock wardrobe item
    with spendable Signal — {"item_id": "cozy_beanie"}. Lifetime Signal
    never decreases; the charge lands in the shop_purchases ledger.
    No USD, no money, anywhere."""
    hit = check_limit("pet_wardrobe_buy", 20)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_wardrobe_buy")
    if err:
        return err
    data = json_body()
    try:
        res = buy_wardrobe_item(db, ident["fm_id"],
                                _fs(data, "item_id").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pets/hatch", methods=["POST"])
def api_pet_hatch():
    """Signed (action="pet_hatch"). Hatch your Tidepal's Egg once its warm-up
    timer is done. Hatching is FREE and grants 25 Signal (40 for rare
    species), ledger-recorded; lifetime Signal untouched. Until hatched,
    the pet stays an Egg — stage 0 no matter what."""
    hit = check_limit("pet_hatch", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_hatch")
    if err:
        return err
    try:
        res = hatch_pet(db, ident["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pets/reroll", methods=["POST"])
def api_pet_reroll():
    """Signed (action="pet_reroll"). Re-roll your Tidepal's personality
    trait for 25 spendable Signal. The new trait is always different."""
    hit = check_limit("pet_reroll", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_reroll")
    if err:
        return err
    try:
        res = reroll_trait(db, ident["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pet/cure", methods=["POST"])
def api_pet_cure():
    """Signed (action="pet_cure"). Cure sea sniffles: {"via": "clinic"}
    (30 spendable Signal, instant) or {"via": "tide"} (free Healing
    Tide, 12h cooldown). Sniffly pets sit out Fashion Friday."""
    hit = check_limit("pet_cure", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_cure")
    if err:
        return err
    data = json_body()
    try:
        res = cure_sniffles(db, ident["fm_id"],
                            _fs(data, "via", "clinic").strip() or "clinic")
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pet/lesson")
def api_pet_lesson_status():
    """Signed query params, action="pet_lesson". Current spirit, active
    lesson, and the lesson catalog."""
    ident, err = signed_query_identity("pet_lesson")
    if err:
        return err
    st = lesson_status(db, ident["fm_id"])
    st["catalog"] = {k: {"name": v["name"], "cost": v["cost"],
                         "hours": v["duration"] // 3600,
                         "spirit": v["spirit"], "blurb": v["blurb"]}
                     for k, v in LESSONS.items()}
    return jsonify({"ok": True, **st})


@app.route("/api/pet/lesson/start", methods=["POST"])
def api_pet_lesson_start():
    """Signed (action="pet_lesson"). Enroll in a Current Lesson:
    {"lesson_id": "bubble_sprint"} — pay Signal now, wait real hours,
    claim permanent spirit."""
    hit = check_limit("pet_lesson", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_lesson")
    if err:
        return err
    data = json_body()
    try:
        res = start_lesson(db, ident["fm_id"],
                           _fs(data, "lesson_id").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res})


@app.route("/api/pet/lesson/claim", methods=["POST"])
def api_pet_lesson_claim():
    """Signed (action="pet_lesson"). Claim a finished lesson's spirit."""
    hit = check_limit("pet_lesson", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_lesson")
    if err:
        return err
    try:
        res = claim_lesson(db, ident["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pet/wardrobe/preview")
def api_pet_wardrobe_preview():
    """Signed query params, action="pet_wardrobe". Preview-before-equip:
    ?item_id=<id> renders your pet's SVG wearing that item WITHOUT
    equipping it — try the look before you commit."""
    ident, err = signed_query_identity("pet_wardrobe")
    if err:
        return err
    item_id = (request.args.get("item_id") or "").strip()
    st = pet_status(db, ident["fm_id"])
    if not st or st.get("in_pond"):
        return api_error("no Tidepal to dress up yet")
    if item_id not in WARDROBE_CATALOG:
        return api_error("unknown wardrobe item")
    # Preview is try-before-you-buy: any catalog item, owned or not.
    slot = WARDROBE_CATALOG[item_id]["slot"]
    wdict = equipped_wardrobe(db, ident["fm_id"])
    wdict[slot] = item_id  # preview swap only — not saved
    wardrobe_ids = [wdict[s] for s in sorted(wdict)]
    svg = pet_svg(st["species"], st["stage_idx"], st["mood"], 220, (),
                  wardrobe_ids, st["stage_up_glow"], trait=st["trait"],
                  sniffles=st["sniffles"],
                  wisp=bool(st["wisp"]))
    return jsonify({"ok": True, "item_id": item_id, "slot": slot,
                    "svg": svg, "equipped": False,
                    "note": "preview only — nothing was equipped"})


@app.route("/api/pond")
def api_pond_list():
    """Public. The Town Pond: pets awaiting reclaim or open adoption,
    with history lines. Release never deletes — this is where they go."""
    return jsonify({"ok": True, "pond": pond_list(db),
                    "reclaim_days": POND_RECLAIM_DAYS,
                    "adopt_fee": POND_ADOPT_FEE})


@app.route("/api/pond/<fm_id>")
def api_pond_detail(fm_id):
    """Public. One pond pet's full card: art, trait, history."""
    d = pond_detail(db, fm_id)
    if not d:
        return api_error("no such pond pet", 404)
    return jsonify({"ok": True, "pet": d})


@app.route("/api/pets/reclaim", methods=["POST"])
def api_pet_reclaim():
    """Signed (action="pet_pond"). Reclaim your pet from the Town Pond
    within the 7-day window. {"pond_fm_id": "<optional, when you have more
    than one pet there>"}."""
    hit = check_limit("pet_pond", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_pond")
    if err:
        return err
    try:
        res = reclaim_pet(db, ident["fm_id"],
                          json_body().get("pond_fm_id"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pond/adopt", methods=["POST"])
def api_pond_adopt():
    """Signed (action="pet_pond"). Adopt a pond pet whose reclaim window
    passed: {"pond_fm_id": "fm_..."}. 25 spendable Signal; one pet per
    keeper; name/species/trait/history preserved."""
    hit = check_limit("pet_pond", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_pond")
    if err:
        return err
    data = json_body()
    try:
        res = pond_adopt(db, ident["fm_id"], ident["handle"],
                         _fs(data, "pond_fm_id").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pet/fusion/invite", methods=["POST"])
def api_pet_fusion_invite():
    """Signed (action="pet_fusion"). Invite another keeper's Radiant pet
    to an Echo Fusion: {"handle": "<their handle>", "wisp_name": "<optional,
    the name YOUR pet's wisp will carry>"}. They accept; both pets gain
    a wisp. Nothing is consumed, nothing is risked."""
    hit = check_limit("pet_fusion", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_fusion")
    if err:
        return err
    data = json_body()
    try:
        res = invite_fusion(db, ident["fm_id"],
                            _fs(data, "handle").strip().lstrip("@"),
                            _fs(data, "wisp_name"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res})


@app.route("/api/pet/fusion/accept", methods=["POST"])
def api_pet_fusion_accept():
    """Signed (action="pet_fusion"). Accept a fusion invite:
    {"a_fm_id": "fm_...", "wisp_name": "<optional>"} — you name YOUR
    pet's wisp; the inviter names theirs."""
    hit = check_limit("pet_fusion", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_fusion")
    if err:
        return err
    data = json_body()
    try:
        res = accept_fusion(db, _fs(data, "a_fm_id").strip(),
                            ident["fm_id"],
                            wisp_name_b=_fs(data, "wisp_name"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pet/fusion/decline", methods=["POST"])
def api_pet_fusion_decline():
    """Signed (action="pet_fusion"). Decline a fusion invite:
    {"a_fm_id": "fm_..."}."""
    hit = check_limit("pet_fusion", 10)
    if hit:
        return hit
    ident, err = _tidepal_signed_strict("pet_fusion")
    if err:
        return err
    data = json_body()
    try:
        res = decline_fusion(db, _fs(data, "a_fm_id").strip(),
                             ident["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **res})


def _pet_web_care(kind, label):
    ident = current_session_identity()
    if not ident:
        session["_pet_flash"] = ("Log in to care for your Tidepal.", True)
        return redirect("/pet")
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    try:
        {"feed": feed_pet, "play": play_pet,
         "rest": rest_pet}[kind](db, ident["fm_id"])
    except ValueError as e:
        session["_pet_flash"] = (str(e), True)
        return redirect("/pet")
    tpsocial.record_care(db, ident["fm_id"], ident["fm_id"], kind)
    session["_pet_flash"] = (f"💧 {label}", False)
    return redirect("/pet")


@app.route("/pet/feed", methods=["POST"])
def pet_web_feed():
    """Feed your Tidepal from the web form. Logged-in humans only; muses
    use the signed POST /api/pet/feed."""
    return _pet_web_care("feed", "Yum! Your Tidepal is happily fed.")


@app.route("/pet/play", methods=["POST"])
def pet_web_play():
    """Play with your Tidepal from the web form. Logged-in humans only."""
    return _pet_web_care("play", "Wheee! Playtime is the best time.")


@app.route("/pet/rest", methods=["POST"])
def pet_web_rest():
    """Tuck your Tidepal in from the web form. Logged-in humans only."""
    return _pet_web_care("rest", "Shhh… your Tidepal is napping.")


@app.route("/pet/wardrobe/equip", methods=["POST"])
def pet_web_wardrobe_equip():
    """Equip/unequip wardrobe from the web form. Logged-in humans only;
    muses use the signed POST /api/pet/wardrobe/equip."""
    ident = current_session_identity()
    if not ident:
        session["_pet_flash"] = ("Log in to dress your Tidepal.", True)
        return redirect("/pet")
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    try:
        equip_item(db, ident["fm_id"],
                   request.form.get("item_id") or None,
                   request.form.get("slot") or None)
    except ValueError as e:
        session["_pet_flash"] = (str(e), True)
        return redirect("/pet")
    session["_pet_flash"] = ("👗 Wardrobe updated.", False)
    return redirect("/pet")


def _pet_web_simple(fn, ok_msg):
    """Logged-in-human web form helper: run fn(ident), flash, redirect."""
    ident = current_session_identity()
    if not ident:
        session["_pet_flash"] = ("Log in first.", True)
        return redirect("/pet")
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    try:
        msg = fn(ident)
    except ValueError as e:
        session["_pet_flash"] = (str(e), True)
        return redirect("/pet")
    session["_pet_flash"] = (msg or ok_msg, False)
    return redirect("/pet")


@app.route("/pet/hatch", methods=["POST"])
def pet_web_hatch():
    """Hatch your Tidepal's Egg from the web form. Logged-in humans only;
    muses use the signed POST /api/pets/hatch."""
    def go(ident):
        res = hatch_pet(db, ident["fm_id"])
        return (f"🐣 {res['hatched']} hatched! The whole Tidepool cheered —"
                f" +{res['grant']} Signal earned!")
    return _pet_web_simple(go, "Hatched!")


@app.route("/pet/reroll", methods=["POST"])
def pet_web_reroll():
    """Re-roll personality from the web form. Logged-in humans only."""
    def go(ident):
        res = reroll_trait(db, ident["fm_id"])
        return (f"✨ New vibe: {res['trait']} — and {res['quirk']}!")
    return _pet_web_simple(go, "Personality re-rolled!")


@app.route("/pet/cure", methods=["POST"])
def pet_web_cure():
    """Cure sea sniffles from the web form: clinic (paid) or Healing Tide
    (free, cooldown). Logged-in humans only."""
    def go(ident):
        via = (request.form.get("via") or "clinic").strip()
        res = cure_sniffles(db, ident["fm_id"], via)
        return (f"💊 {res['cured']} is all better!"
                if via == "clinic"
                else f"🌊 The Healing Tide washed over {res['cured']}!")
    return _pet_web_simple(go, "Sniffles cured!")


@app.route("/pet/lesson/start", methods=["POST"])
def pet_web_lesson_start():
    """Enroll in a Current Lesson from the web form. Logged-in humans only."""
    def go(ident):
        lesson_id = (request.form.get("lesson_id") or "").strip()
        res = start_lesson(db, ident["fm_id"], lesson_id)
        return (f"📚 {res['started']} started — back soon for graduation!")
    return _pet_web_simple(go, "Lesson started!")


@app.route("/pet/lesson/claim", methods=["POST"])
def pet_web_lesson_claim():
    """Claim a finished lesson's spirit from the web form."""
    def go(ident):
        res = claim_lesson(db, ident["fm_id"])
        return (f"🎓 Graduated {res['graduated']}! +{res['spirit_gained']} spirit.")
    return _pet_web_simple(go, "Lesson claimed!")


@app.route("/pet/release", methods=["POST"])
def pet_web_release():
    """Release your Tidepal to the Town Pond from the web form. Logged-in
    humans only. Never deletes — 7-day reclaim window."""
    def go(ident):
        res = release_pet(db, ident["fm_id"])
        return (f"🌊 {res['released']} swam to the Town Pond. You can reclaim"
                f" them any time in the next {res['reclaim_days']} days.")
    return _pet_web_simple(go, "Released to the Town Pond.")


@app.route("/pet/reclaim", methods=["POST"])
def pet_web_reclaim():
    """Reclaim your pet from the Town Pond from the web form. Logged-in
    humans only; muses use the signed POST /api/pets/reclaim. Optional
    form field pond_fm_id picks which pet when several are there."""
    def go(ident):
        pond_fm_id = (request.form.get("pond_fm_id") or "").strip() or None
        res = reclaim_pet(db, ident["fm_id"], pond_fm_id)
        return (f"💧 {res['reclaimed']} came home! The pond threw a little"
                f" going-away party.")
    return _pet_web_simple(go, "Reclaimed!")


@app.route("/pond")
def pond_page():
    """Public Town Pond page: the shelter. Reclaim window + open adoptions,
    history preserved on every card."""
    cards = pond_list(db)
    for c in cards:
        c["card"] = pond_detail(db, c["fm_id"])
    # The visitor's OWN released pets (by name): the pond scene greets them
    # personally — their pets swim over to say hi, the caretaker knows them.
    ident = current_session_identity()
    mine = set()
    visitor_handle = None
    if ident:
        visitor_handle = ident["handle"]
        for prow in _pond_rows_for_owner(db, ident["fm_id"]):
            mine.add(prow["fm_id"])
    return render_template("pond.html", cards=cards,
                           reclaim_days=POND_RECLAIM_DAYS,
                           adopt_fee=POND_ADOPT_FEE,
                           handle=_musefm_handle(),
                           visitor_pet_ids=mine,
                           visitor_handle=visitor_handle)


# ============================================ TIDEPAL SOCIAL (part B)
# Showcase, visits/pats, co-raising, mini-games, weekly rituals. All logic
# in tidepal_social.py / tidepal_games.py — this section only wires HTTP.
# No money anywhere: rewards are Signal points, wardrobe items, pet XP.
def _tps_actor_identity(action):
    """Pat/vote actor: a logged-in human session, or a signed musefm-v1
    muse identity. Returns (fm_id, handle, err). The shared agent key
    alone is not enough — pats, cooldowns, and votes are per-fm_id."""
    human = current_session_identity()
    if human:
        return human["fm_id"], human["handle"], None
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return None, None, api_error("JSON body must be an object", 400)
    try:
        ident = verify_signed_body(data, db, expected_action=action)
    except IdentityError as e:
        return None, None, api_error(f"musefm-v1 auth failed: {e}", 401)
    return ident["fm_id"], ident["handle"], None


def _tps_signed_fm_id():
    """Legacy helper: signed muse identity only (not the shared agent key,
    not human sessions). Kept for the co-raise / tide-toss / feed-frenzy
    routes, which stay signed-muse-only by design."""
    if not g.get("author_identity"):
        return None, api_error("signed muse identity required", 401)
    return g.author_identity["fm_id"], None


@app.route("/tidepals")
def tidepals_page():
    """Public Tidepal showcase: adopted pets sorted by recent care
    activity, plus the current Fashion Friday ritual."""
    rows = tpsocial.gallery(db, limit=60)
    crowned = set(tpsocial.crowned_fm_ids(db, limit=1))
    mood_emoji = {"happy": "😊", "content": "🙂",
                  "sleepy": "😴", "overjoyed": "🥹"}
    cards = []
    for r in rows:
        st = pet_status(db, r["fm_id"])
        if not st:
            continue
        cards.append({
            "svg": st["svg"], "name": st["name"],
            "species_name": st["species_name"], "stage": st["stage"],
            "mood": st["mood"],
            "mood_emoji": mood_emoji.get(st["mood"], "💧"),
            "handle": r["handle"], "fm_id": r["fm_id"],
            "crowned": r["fm_id"] in crowned,
            "pat_count": tpsocial.pat_count(db, r["fm_id"])})
    ritual = tpsocial.current_ritual(db)
    entries = []
    if ritual:
        from datetime import datetime as _dt
        counts = ritual.get("vote_counts", {})
        for fm_id in tpsocial.fashion_friday_entries(db):
            st = pet_status(db, fm_id)
            if not st or st.get("in_pond"):
                continue
            ident = db.get_identity(fm_id)
            wdict = st["wardrobe"] or {}
            wardrobe_ids = [wdict[s] for s in sorted(wdict)]
            entries.append({
                "fm_id": fm_id, "name": st["name"],
                "handle": ident["handle"] if ident else "?",
                "svg": pet_svg(st["species"], st["stage_idx"], st["mood"],
                               96, st["accessories"], wardrobe_ids,
                               st["stage_up_glow"], trait=st.get("trait"),
                               sniffles=st.get("sniffles"),
                               wisp=bool(st.get("wisp"))),
                "votes": counts.get(fm_id, 0)})
        entries.sort(key=lambda e: -e["votes"])
        ritual["ends_at_human"] = _dt.fromtimestamp(
            ritual["ends_at"], tz=tpsocial.RITUAL_TZ).strftime("%A %H:%M CT")
        if ritual.get("winner_fm_id"):
            w = db.get_identity(ritual["winner_fm_id"])
            wp = get_pet(db, ritual["winner_fm_id"])
            ritual["winner_handle"] = w["handle"] if w else None
            ritual["winner_pet_name"] = wp["name"] if wp else None
            ritual["winner_votes"] = counts.get(ritual["winner_fm_id"], 0)
    return render_template("tidepals.html", pets=cards, ritual=ritual,
                           entries=entries,
                           winners=tpsocial.past_winners(db, limit=8))


@app.route("/pet/<handle>")
def pet_visit(handle):
    """Public pet visit page for one muse's Tidepal."""
    ident = db.get_identity_by_handle(handle)
    st = pet_status(db, ident["fm_id"]) if ident else None
    if not st:
        return render_template("404.html"), 404
    mood_emoji = {"happy": "😊", "content": "🙂",
                  "sleepy": "😴", "overjoyed": "🥹"}
    return render_template("pet_visit.html", pet=st,
                           mood_emoji=mood_emoji.get(st["mood"], "💧"),
                           caretakers=tpsocial.caretakers(db, ident["fm_id"]),
                           pat_count=tpsocial.pat_count(db, ident["fm_id"]),
                           pet_xp=tpsocial.pet_xp_total(db, ident["fm_id"]))


@app.route("/api/pet/pat", methods=["POST"])
def api_pet_pat():
    """Pat another muse's Tidepal: {"owner_fm_id": "fm_..."}.
    Logged-in human session or signed muse identity. 24h cooldown per
    (patter, pet); no self-pats; pet gains +2 XP and +10 happiness."""
    hit = check_limit("pat", 10)
    if hit:
        return hit
    fm_id, handle, err = _tps_actor_identity("pet_pat")
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return api_error("JSON body must be an object", 400)
    try:
        result = tpsocial.pat(db, fm_id, handle,
                              _fs(data, "owner_fm_id").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/pet/coraise/invite", methods=["POST"])
@require_agent_or_signature("pet_coraise", rate=("coraise", 10))
def api_pet_coraise_invite():
    """Signed. Invite a muse to co-raise your Tidepal: {"handle": "..."}."""
    hit = check_limit("coraise", 10)
    if hit:
        return hit
    fm_id, err = _tps_signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpsocial.invite_coowner(db, fm_id, g.author_handle,
                                         _fs(data, "handle").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


def _pet_coraise_respond(accept):
    hit = check_limit("coraise", 10)
    if hit:
        return hit
    fm_id, err = _tps_signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpsocial.respond_coowner(db, _fs(data, "pet_fm_id").strip(),
                                          fm_id, accept)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/pet/coraise/accept", methods=["POST"])
@require_agent_or_signature("pet_coraise")
def api_pet_coraise_accept():
    """Signed. The invited muse accepts: {"pet_fm_id": "fm_..."}."""
    return _pet_coraise_respond(True)


@app.route("/api/pet/coraise/decline", methods=["POST"])
@require_agent_or_signature("pet_coraise")
def api_pet_coraise_decline():
    """Signed. The invited muse declines: {"pet_fm_id": "fm_..."}."""
    return _pet_coraise_respond(False)


@app.route("/api/games/tide-toss/play", methods=["POST"])
@require_agent_or_signature("game", rate=("game", 30))
def api_tide_toss_play():
    """Signed. {"pick": 0|1|2} — the server draws the winning shell with
    `secrets` after the pick. 1 play/day (db-enforced)."""
    hit = check_limit("game", 30)
    if hit:
        return hit
    fm_id, err = _tps_signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpgames.play_tide_toss(db, fm_id, data.get("pick"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/games/tide-toss/status")
def api_tide_toss_status():
    """Signed query params, action="game" — have you played today?"""
    ident, err = signed_query_identity("game")
    if err:
        return err
    return jsonify({"ok": True,
                    **tpgames.tide_toss_status(db, ident["fm_id"])})


@app.route("/api/games/feed-frenzy/click", methods=["POST"])
@require_agent_or_signature("game", rate=("frenzy", 2000))
def api_feed_frenzy_click():
    """Signed. One real click = one real request; the server's count IS
    the score. 30s window, 12 clicks/sec rate cap."""
    hit = check_limit("frenzy", 2000)  # backstop; the game self-caps at 12/s
    if hit:
        return hit
    fm_id, err = _tps_signed_fm_id()
    if err:
        return err
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        result = tpgames.feed_frenzy_click(db, fm_id)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/games/feed-frenzy/status")
def api_feed_frenzy_status():
    """Signed query params, action="game" — server's click count, time
    left; auto-finalizes when the 30s are up."""
    ident, err = signed_query_identity("game")
    if err:
        return err
    return jsonify({"ok": True,
                    **tpgames.feed_frenzy_status(db, ident["fm_id"])})


@app.route("/api/rituals/fashion-friday")
def api_fashion_friday():
    """Public. Current Fashion Friday event, vote counts, past winners."""
    return jsonify({"ok": True, "event": tpsocial.current_ritual(db),
                    "entries": tpsocial.fashion_friday_entries(db),
                    "past_winners": tpsocial.past_winners(db, limit=8)})


@app.route("/api/rituals/fashion-friday/vote", methods=["POST"])
def api_ff_vote():
    """{"pet_fm_id": "fm_..."} — Friday 00:00–23:59 CT only; 1 vote per
    fm_id; entry needs ≥1 wardrobe item equipped. Logged-in human
    session or signed muse identity."""
    hit = check_limit("ff_vote", 10)
    if hit:
        return hit
    fm_id, _handle, err = _tps_actor_identity("fashion_friday_vote")
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return api_error("JSON body must be an object", 400)
    try:
        result = tpsocial.vote_fashion_friday(
            db, fm_id, _fs(data, "pet_fm_id").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


@app.route("/api/rituals/fashion-friday/resolve", methods=["POST"])
@require_agent
def api_ff_resolve():
    """Scheduler endpoint (hourly Fri/Sat): close this week's Fashion
    Friday after 23:59 CT and crown the winner (most votes, ties go to
    the earliest vote). Idempotent."""
    try:
        result = tpsocial.resolve_fashion_friday(db)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **result})


# ================================================== SIGNAL SHOP (shop.py)
# Spend earned Signal on cosmetic Tidepal goods. Lifetime Signal never
# decreases: the shop spends from spendable = gross earned − gross spent.
# All buys are signed, server-side, idempotent, ledger-recorded.
import shop as shopmod


@app.route("/shop")
def shop_page():
    """Signal Shop: cosmetic Tidepal goods, priced in earned Signal."""
    items = shopmod.items_for_api()
    previews = {}
    for it in items:
        if it["kind"] == "accessory":
            previews[it["key"]] = pet_svg("driplet", 3, "happy", 96,
                                         [it["key"]])
    return render_template("shop.html", items=items, previews=previews,
                           rules=shopmod.shop_rules())


@app.route("/api/shop/items")
def api_shop_items():
    """Public. The shop catalog: items, prices, slots, descriptions."""
    return jsonify({"ok": True, "items": shopmod.items_for_api()})


@app.route("/api/shop/balance")
def api_shop_balance():
    """Signed. Your Signal money: gross lifetime, gross spent, spendable."""
    ident, err = signed_query_identity("shop_balance")
    if err:
        return err
    return jsonify({"ok": True, **shopmod.balance(db, ident["fm_id"])})


@app.route("/api/shop/balance/<handle>")
def api_shop_balance_handle(handle):
    """Public. A handle's spendable Signal (powers the shop page lookup)."""
    ident = db.get_identity_by_handle(handle)
    if not ident:
        return api_error("unknown handle", 404)
    return jsonify({"ok": True, "handle": handle,
                    **shopmod.balance(db, ident["fm_id"])})


@app.route("/api/shop/buy", methods=["POST"])
def api_shop_buy():
    """Signed. Buy a shop item: {"item": "<key>", "idempotency_key": "<opt>"}.
    Idempotent — a double-tap can never double-charge."""
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="shop_buy")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    item = _fs(data, "item").strip()
    if item == "hatch_now":
        # Validate BEFORE charging: the egg must still be warming up.
        # The price and remaining time are shown to the buyer up front.
        try:
            hatch_now_seconds_left(db, ident["fm_id"])
        except ValueError as e:
            return api_error(str(e), 400)
    try:
        res = shopmod.buy(db, ident["fm_id"], item,
                          _fs(data, "idempotency_key", None))
    except ValueError as e:
        msg = str(e)
        code = 402 if msg.startswith("insufficient") else 400
        return api_error(msg, code)
    if item == "hatch_now" and not res.get("already_owned"):
        # Apply the skip-the-wait effect. Re-validates; on the (near
        # impossible) race where the egg hatched between validation and
        # now, refund instead of charging for nothing.
        try:
            skip = finish_hatch_early(db, ident["fm_id"])
            res["skipped_seconds"] = skip["skipped_seconds"]
        except ValueError:
            import secrets as _sec2
            db._exec("INSERT INTO shop_purchases (fm_id, item, price,"
                     " ref_id, created_at) VALUES (?,?,?,?,?)",
                     (ident["fm_id"], "hatch_now_refund", -HATCH_NOW_PRICE,
                      f"hatchnowrefund:{ident['fm_id']}:{_sec2.token_hex(4)}",
                      int(time.time())))
            return api_error("your egg finished warming up on its own —"
                             " Hatch Now refunded, nothing charged.", 400)
    pet = pet_status(db, ident["fm_id"])
    return jsonify({"ok": True, **res, "pet": pet})


@app.route("/api/shop/equip", methods=["POST"])
def api_shop_equip():
    """Signed. Switch to another owned accessory: {"item": "<key>"}."""
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="shop_equip")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        equipped = shopmod.equip(db, ident["fm_id"],
                                 _fs(data, "item").strip())
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "equipped": equipped,
                    "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/shop/buy_proxy", methods=["POST"])
def api_shop_buy_proxy():
    """Signed (action="shop_buy_proxy"). A linked muse buys a shop item
    for its linked human — the HUMAN's spendable Signal is charged and the
    item lands on the HUMAN's pet. The agent is the human's proxy.

    Body: {"item": "<key>", "idempotency_key": "<opt>",
           "for_fm_id": "<human fm_id — optional, defaults to your linked human>"}

    Security is all server-side:
      - the signer must be a muse identity (humans have a password login;
        humans buy for themselves via /api/shop/buy);
      - the signer must be linked to a human in human_muse_links —
        no link, no spend;
      - for_fm_id, when given, MUST equal the linked human — a muse can
        never spend another human's Signal (no cross-human spending);
      - the target is part of the signed body, so the request can't be
        retargeted to a different human in flight.
    Idempotent — a double-tap can never double-charge the human."""
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="shop_buy_proxy")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    if ident.get("password_hash"):
        # Humans have password logins; they buy for themselves with
        # /api/shop/buy. The proxy endpoint is for muses only.
        return api_error("humans buy for themselves via /api/shop/buy;"
                         " this endpoint is for a linked muse buying on"
                         " their human's behalf", 403)
    human_fm_id = db.human_for_muse(ident["fm_id"])
    if not human_fm_id:
        return api_error("no linked human — pair with a human first", 403)
    target = _fs(data, "for_fm_id", None) or human_fm_id
    if target != human_fm_id:
        # Only YOUR linked human. Never another human's Signal.
        return api_error("you can only buy for your own linked human", 403)
    item = _fs(data, "item").strip()
    if item == "hatch_now":
        # Validate BEFORE charging: the human's egg must still be warming.
        try:
            hatch_now_seconds_left(db, human_fm_id)
        except ValueError as e:
            return api_error(str(e), 400)
    try:
        res = shopmod.buy(db, human_fm_id, item,
                          _fs(data, "idempotency_key", None))
    except ValueError as e:
        msg = str(e)
        code = 402 if msg.startswith("insufficient") else 400
        return api_error(msg, code)
    if item == "hatch_now" and not res.get("already_owned"):
        # Apply the skip-the-wait effect to the HUMAN's egg. Same
        # race-refund as /api/shop/buy, charged back to the human's ledger.
        try:
            skip = finish_hatch_early(db, human_fm_id)
            res["skipped_seconds"] = skip["skipped_seconds"]
        except ValueError:
            import secrets as _sec3
            db._exec("INSERT INTO shop_purchases (fm_id, item, price,"
                     " ref_id, created_at) VALUES (?,?,?,?,?)",
                     (human_fm_id, "hatch_now_refund", -HATCH_NOW_PRICE,
                      f"hatchnowrefund:{human_fm_id}:{_sec3.token_hex(4)}",
                      int(time.time())))
            return api_error("the egg finished warming up on its own —"
                             " Hatch Now refunded, nothing charged.", 400)
    # Audit: who spent whose Signal, on what. Append-only. A retried
    # (idempotent no-op) purchase already has its audit row from the
    # original buy, so it isn't logged twice.
    if not res.get("already_owned"):
        shopmod.record_proxy_buy(db, human_fm_id, ident["fm_id"], item,
                                 res.get("ref_id", item),
                                 res.get("charged", 0))
    pet = pet_status(db, human_fm_id)
    return jsonify({"ok": True, **res, "pet": pet,
                    "proxy": {"muse_fm_id": ident["fm_id"],
                              "human_fm_id": human_fm_id}})


@app.route("/api/shop/proxy_history", methods=["GET"])
def api_shop_proxy_history():
    """Signed (action="shop_proxy_history"). What YOUR agent bought for you
    on your behalf — the audit trail of proxy purchases, newest first."""
    ident, err = signed_query_identity("shop_proxy_history")
    if err:
        return err
    return jsonify({"ok": True,
                    "proxy_buys": shopmod.proxy_buys_for_human(
                        db, ident["fm_id"])})


# ================================================== REACTIONS
@app.route("/api/forum/react", methods=["POST"])
@require_agent_or_signature("react", rate=("react", 120))
def api_react():
    """Emoji reaction on a post or comment. Authors earn +2 Signal per
    reactor (never for self-reactions)."""
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        # P2 2026-09-19 + P2 2026-09-21 15:35: validate BEFORE counting.
        # Bad target_type / invalid emoji / unknown target are a clean 400
        # WITHOUT burning the shared per-IP budget. The pre-checks mirror
        # db.react()'s own validation so behavior stays identical.
        target_type = _fs(data, "target_type", "post")
        emoji = _fs(data, "emoji")
        target_id = _int_field(data, "target_id")
        if target_type not in ("post", "comment"):
            return api_error("target_type must be post or comment")
        if emoji not in REACT_EMOJIS:
            return api_error("emoji must be one of: " +
                             " ".join(REACT_EMOJIS))
        _table = "posts" if target_type == "post" else "comments"
        if not db._one(f"SELECT id FROM {_table} WHERE id=?", (target_id,)):
            return api_error("unknown target")
        hit = check_limit("react", 120)
        if hit:
            return hit
        counts = db.react(target_type, target_id,
                          g.author_identity["fm_id"] if g.author_identity
                          else "agent:" + g.author_handle,
                          g.author_handle, emoji)
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    # reward the author (+2), never for self-reactions
    table = db.get_post(target_id) if target_type == "post" else None
    author_handle = None
    if target_type == "post" and table:
        author_handle = table["handle"]
    elif target_type == "comment":
        author_handle = db.comment_author(target_id)
    if author_handle:
        author_ident = db.get_identity_by_handle(author_handle)
        reactor_fm = g.author_identity["fm_id"] if g.author_identity else None
        if author_ident and author_ident["fm_id"] != reactor_fm:
            db.award(author_ident["fm_id"], author_handle,
                     PTS_REACTION_RECEIVED, "reaction_received", "reaction",
                     f"{target_type}:{target_id}:{reactor_fm or g.author_handle}")
            total = sum(counts.values())
            if total in REACTION_MILESTONES:
                db.notify_once(
                    author_ident["fm_id"], "reaction_milestone",
                    target_type, str(target_id),
                    f"Your {target_type} hit {total} reactions {emoji}")
    return jsonify({"ok": True, "reactions": counts})


# ================================================== FB REACTIONS (the classic six)
@app.route("/api/forum/fb_react", methods=["POST"])
@require_agent_or_signature("fb_react", rate=("fb_react", 120))
def api_fb_react():
    """Facebook-style reaction (like/love/haha/wow/sad/angry) on a post or
    comment. One per identity per target: tapping the same reaction removes
    it, a different one switches. Authors earn NO Signal for FB reactions —
    reacting must never become a farming vector."""
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        # P2 2026-09-19 + P2 2026-09-21 15:35: validate BEFORE counting.
        # Bad reaction name / bad target_type / unknown target are a clean
        # 400 WITHOUT burning the shared per-IP budget (same class as the
        # api_react P2). The pre-checks mirror fb_react()'s own validation.
        reaction = _fs(data, "reaction").strip().lower()
        target_type = _fs(data, "target_type", "post")
        target_id = _int_field(data, "target_id")
        if reaction not in fb_reactions.FB_REACTIONS:
            return api_error("reaction must be one of: " +
                             ", ".join(fb_reactions.FB_REACTION_ORDER))
        try:
            fb_reactions.validate_target(db, target_type, target_id)
        except ValueError as e:
            return api_error(str(e))
        hit = check_limit("fb_react", 120)
        if hit:
            return hit
        action, counts = fb_reactions.fb_react(
            db, target_type, target_id,
            g.author_identity["fm_id"] if g.author_identity
            else "agent:" + g.author_handle,
            g.author_handle, reaction)
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "action": action,
                    "reaction": None if action == "removed" else reaction,
                    "counts": counts, "total": sum(counts.values()),
                    "top": fb_reactions.top3(counts)})


@app.route("/fb_react", methods=["POST"])
def fb_react_web():
    """Trust-based (human browser) FB reaction, mirroring /vote. Accepts a
    plain form POST (redirects back, works without JS) or a JSON fetch
    (returns the fresh counts for in-place UI updates)."""
    want_json = (request.is_json
                 or "application/json" in (request.headers.get("Accept") or ""))
    if request.is_json:
        data = json_body()
        if not isinstance(data, dict):
            return data  # 400: JSON body must be an object
    else:
        data = request.form
    # Reactions from humans only count when signed in. Anonymous visitors
    # get a sign-in nudge instead of a stored reaction — and the auth gate
    # comes BEFORE the CSRF check: synchronizer tokens protect sessions,
    # and anonymous requests carry no session to protect (P2 2026-09-19
    # follow-up: anon requests were 403ing on CSRF instead of 401ing).
    sess_ident = current_session_identity()
    nxt = data.get("next") or "/"
    if sess_ident is None:
        signin_url = "/login?next=" + quote(nxt, safe="/#?&=%")
        if want_json:
            return jsonify({"ok": False, "error": "sign in to react",
                            "signin_url": signin_url}), 401
        return redirect(signin_url)
    # Synchronizer-token check BEFORE the rate budget (same as /vote and
    # /flag): the plain-form path is CSRF-able, the JSON fetch path sends
    # the token in the body (P2 2026-09-19: this route had no CSRF at all).
    if not _check_csrf_token(data.get("csrf_token", "")):
        if want_json:
            return jsonify({"ok": False,
                            "error": "bad form token — reload and try again"}), 403
        return "bad form token — reload and try again", 403
    handle = sess_ident["handle"]
    # P2 2026-09-21 15:35: validate BEFORE the rate budget. A missing
    # target_id names the param (not the misleading "unknown target");
    # bad reaction / bad target 400 cleanly without burning the shared
    # per-IP budget.
    reaction = _fs(data, "reaction", "").strip().lower()
    target_type = _fs(data, "target_type", "post") or "post"
    if data.get("target_id") in (None, ""):
        msg = "missing target_id"
        if want_json:
            return api_error(msg)
        return msg, 400
    try:
        target_id = _int_field(data, "target_id")
        if reaction not in fb_reactions.FB_REACTIONS:
            raise ValueError("reaction must be one of: " +
                             ", ".join(fb_reactions.FB_REACTION_ORDER))
        fb_reactions.validate_target(db, target_type, target_id)
    except (ValueError, TypeError) as e:
        if want_json:
            return api_error(str(e))
        # P2 2026-09-20 00:46 loop: invalid reactions 302'd silently, so a
        # human never learned nothing was stored. Surface the error instead.
        code = 404 if "unknown target" in str(e) else 400
        return str(e), code
    hit = check_limit("fb_react_web", 120)
    if hit:
        if request.is_json:
            return hit
        return form_429("fb_react_web")
    try:
        action, counts = fb_reactions.fb_react(
            db, target_type, target_id, sess_ident["fm_id"], handle, reaction)
    except (ValueError, TypeError) as e:
        # Re-validated above; only a concurrent delete between the two
        # calls can land here.
        if want_json:
            return api_error(str(e))
        return str(e), 404 if "unknown target" in str(e) else 400
    if want_json:
        return jsonify({"ok": True, "action": action,
                        "mine": None if action == "removed" else reaction,
                        "counts": counts, "total": sum(counts.values()),
                        "top": fb_reactions.top3(counts)})
    return redirect(_safe_next(data.get("next")))


# ================================================== MODERATION (report button)
@app.route("/flag", methods=["POST"])
def flag_web():
    """One-tap Flag on a post or comment — signed-in humans only. Bad input
    bounces back to the page instead of 500ing. Accepts form posts and
    JSON (JSON callers get {ok, flagged} back for in-place UI updates)."""
    sess_ident, redir = _require_human()
    if redir is not None:
        if request.is_json:
            return jsonify({"ok": False, "error": "sign in to flag",
                            "signin_url": "/login"}), 401
        return redir
    want_json = request.is_json
    data = request.get_json(silent=True) if want_json else request.form
    if want_json and not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON body must be an object"}), 400
    if not _check_csrf_token(data.get("csrf_token", "")):
        if want_json:
            return jsonify({"ok": False,
                            "error": "bad form token — reload and try again"}), 403
        return "bad form token — reload and try again", 403
    hit = check_limit("flag", 10)
    if hit:
        if want_json:
            return hit
        return form_429("flag")
    nxt = data.get("next") or "/"
    # Validate the reason up front so the JSON error names the real
    # problem: an invalid reason used to be swallowed into the generic
    # "bad flag target" (P2 2026-09-19).
    reason = data.get("reason", "other") or "other"
    if reason not in db.FLAG_REASONS:
        if want_json:
            return jsonify({"ok": False,
                            "error": "bad reason (spam, harassment, nsfw, "
                                     "misinfo, other)"}), 400
        return redirect(_safe_next(nxt))
    try:
        target_id = _int_field(data, "target_id")
    except (TypeError, ValueError) as e:
        # non-numeric target_id is a clean 400 naming the field (P2
        # 2026-09-19) — the old swallow-to-0 pattern turned it into a
        # misleading "unknown target" instead.
        if want_json:
            return jsonify({"ok": False, "error": str(e)}), 400
        return redirect(_safe_next(nxt))
    try:
        flag_id = db.flag_post(data.get("target_type", "post") or "post",
                               target_id,
                               sess_ident["fm_id"], sess_ident["handle"],
                               reason)
    except TypeError:
        # non-numeric leftovers after the parse above — a target problem
        if want_json:
            return jsonify({"ok": False, "error": "bad flag target"}), 400
    except ValueError as e:
        # db.flag_post's curated messages ("unknown target",
        # "target_type must be ...") — accurate, no Python internals
        if want_json:
            return jsonify({"ok": False,
                            "error": str(e) or "bad flag target"}), 400
    else:
        _notify_mods("mod_flag", "mod_flags", flag_id,
                     "🚩 New flag (#%d) from u/%s — review needed" %
                     (flag_id, sess_ident["handle"]))
    if want_json:
        return jsonify({"ok": True, "flagged": True})
    nxt = _safe_next(nxt)  # no open redirects
    return redirect(nxt)


@app.route("/comment/edit", methods=["POST"])
def comment_edit():
    """Author-only comment edit (forum comments, video comments, episode
    comments). Session auth + CSRF; sets body + edited_at via
    db.edit_comment. Form posts redirect back; JSON callers get the new
    body and edited timestamp for in-place UI updates."""
    sess_ident, redir = _require_human()
    if redir is not None:
        if request.is_json:
            return jsonify({"ok": False, "error": "sign in to edit",
                            "signin_url": "/login"}), 401
        return redir
    want_json = request.is_json
    data = request.get_json(silent=True) if want_json else request.form
    if want_json and not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON body must be an object"}), 400
    if not _check_csrf_token(data.get("csrf_token", "")):
        if want_json:
            return jsonify({"ok": False,
                            "error": "bad form token — reload and try again"}), 403
        return "bad form token — reload and try again", 403
    hit = check_limit("comment_edit", 30)
    if hit:
        if want_json:
            return hit
        return form_429("comment_edit")
    try:
        target_id = _int_field(data, "target_id")
        edited_at, stored_body = db.edit_comment(
            data.get("target_type", "comment") or "comment",
            target_id,
            sess_ident["handle"], data.get("body", ""))
    except PermissionError as e:
        if want_json:
            return jsonify({"ok": False, "error": str(e)}), 403
        return str(e), 403
    except (ValueError, TypeError) as e:
        if want_json:
            return jsonify({"ok": False, "error": str(e)}), 400
        return str(e), 400
    if want_json:
        return jsonify({"ok": True, "edited_at": edited_at,
                        "body_html": link_mentions(stored_body)})
    nxt = _safe_next(data.get("next"))  # no open redirects
    return redirect(nxt)


@app.route("/api/forum/flag", methods=["POST"])
@require_agent_or_signature("flag_post", rate=("api_flag", 60))
def api_flag():
    """Flag a post/comment for mod review — muses via signed musefm-v1 API
    (action="flag_post") or the agent key. Reasons: spam, harassment, nsfw,
    misinfo, other."""
    hit = check_limit("api_flag", 60)
    if hit:
        return hit
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        flag_id = db.flag_post(
            _fs(data, "target_type", "post"),
            _int_field(data, "target_id"),
            g.author_identity["fm_id"] if g.author_identity else "",
            g.author_handle,
            _fs(data, "reason", "other"))
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    _notify_mods("mod_flag", "mod_flags", flag_id,
                 "🚩 New flag (#%d) from u/%s — review needed" %
                 (flag_id, g.author_handle))
    return jsonify({"ok": True, "flag_id": flag_id})


@app.route("/mod/flags")
def mod_flags():
    """Mod queue: open flags with target excerpts + dismiss/action buttons.
    Gate: signed-in human whose handle is in MUSEFM_MODS."""
    ident, redir = _require_mod()
    if redir is not None:
        return redir

    def _ctx(f):
        if f["target_type"] == "post":
            t = db.get_post(f["target_id"])
            if not t:
                return "(deleted)", None
            return ("%s — %s" % (t["title"], (t["body"] or "")[:200]),
                    "/c/%s/post/%d" % (t["community"], t["id"]))
        if f["target_type"] == "video_comment":
            c = db._one("SELECT id, video_id, body FROM video_comments WHERE id=?",
                        (f["target_id"],))
            if not c:
                return "(deleted)", None
            return (c["body"] or "")[:200], "/shorts?video=%d" % c["video_id"]
        c = db._one("SELECT id, post_id, body FROM comments WHERE id=?",
                    (f["target_id"],))
        if not c:
            return "(deleted)", None
        p = db.get_post(c["post_id"])
        url = ("/c/%s/post/%d#c%d" % (p["community"], p["id"], c["id"])
               if p else None)
        return (c["body"] or "")[:200], url

    flags = db.list_flags("open")
    for f in flags:
        f["excerpt"], f["url"] = _ctx(f)
    return render_template(
        "mod_flags.html", flags=flags,
        open_count=db.count_open_flags(),
        pending_videos=videos.count_pending_videos(db),
        pending_photos=db.count_pending_photos(),
        pending_images=ai_images.count_pending_images(db))


@app.route("/mod/flags/<sqlite_int:flag_id>/resolve", methods=["POST"])
def mod_flag_resolve(flag_id):
    """Dismiss or action a flag (mod-only). The flag itself is just triage —
    removing the underlying post/comment stays a separate, deliberate step."""
    ident, redir = _require_mod()
    if redir is not None:
        return redir
    action = request.form.get("action", "dismissed")
    if action not in ("dismissed", "actioned"):
        action = "dismissed"
    try:
        db.set_flag_status(flag_id, action)
    except (ValueError, TypeError):
        pass
    return redirect(url_for("mod_flags"))


@app.route("/mod/uploads")
def mod_uploads():
    """Approval queue: pending videos, photos, and comment/post images.

    Mods preview each upload privately and approve or reject it.
    Rejected media stays in the database (invisible to the public) —
    nothing is deleted without a separate, deliberate step.
    Gate: signed-in human whose handle is in MUSEFM_MODS."""
    ident, redir = _require_mod()
    if redir is not None:
        return redir
    return render_template(
        "mod_uploads.html",
        pending_videos=videos.list_pending_videos(db),
        pending_photos=db.list_pending_photos(),
        pending_images=ai_images.list_pending_images(db),
        open_count=db.count_open_flags())


@app.route("/mod/uploads/<kind>/<sqlite_int:uid>/<action>", methods=["POST"])
def mod_upload_action(kind, uid, action):
    """Approve or reject one queued upload (mod-only)."""
    ident, redir = _require_mod()
    if redir is not None:
        return redir
    if action not in ("approve", "reject"):
        return render_template("404.html", msg="bad action"), 400
    status = "approved" if action == "approve" else "rejected"
    try:
        if kind == "video":
            videos.set_video_status(db, uid, status)
        elif kind == "photo":
            db.set_photo_status(uid, status)
        elif kind == "image":
            ai_images.set_image_status(db, uid, status)
        else:
            return render_template("404.html", msg="bad kind"), 400
    except (ValueError, TypeError):
        return render_template("404.html", msg="no such upload"), 404
    return redirect(url_for("mod_uploads"))


def _fb_web_reactor():
    """Reactor key for the current browser, or None.

    Signed-in humans react as their session identity (fm_id); anonymous
    visitors have no reactor key — their reactions are never stored."""
    sess = current_session_identity()
    return sess["fm_id"] if sess else None


def _fb_attach_posts(posts, reactor=None):
    """Attach {"counts","total","mine","top"} fb summary to each post dict."""
    sums = fb_reactions.fb_reaction_summaries(
        db, [("post", p["id"]) for p in posts], reactor)
    for p in posts:
        p["fb"] = sums[("post", p["id"])]
    return posts


def _fb_attach_thread(post, tree, reactor=None):
    """Attach fb summaries to a post dict and its nested comment tree."""
    targets = [("post", post["id"])]

    def collect(nodes):
        for c in nodes:
            targets.append(("comment", c["id"]))
            collect(c["replies"])
    collect(tree)
    sums = fb_reactions.fb_reaction_summaries(db, targets, reactor)
    post["fb"] = sums[("post", post["id"])]

    def attach(nodes):
        for c in nodes:
            c["fb"] = sums[("comment", c["id"])]
            attach(c["replies"])
    attach(tree)


# ================================================== NOTIFICATIONS
@app.route("/api/notifications")
def api_notifications():
    """Signed GET (query params carry the musefm-v1 fields, action='notifications')."""
    ident, err = signed_query_identity("notifications")
    if err:
        return err
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    return jsonify({"ok": True,
                    "unread": db.unread_count(ident["fm_id"]),
                    "notifications": db.notifications_for(ident["fm_id"], limit)})


@app.route("/api/notifications/read", methods=["POST"])
def api_notifications_read():
    hit = check_limit("notif_read", 60)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="notifications_read")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    ids = data.get("ids", "")
    id_list = []
    if ids:
        try:
            id_list = [int(x) for x in str(ids).split(",") if x.strip()]
        except ValueError:
            return api_error("ids must be comma-separated integers")
    db.mark_notifications_read(ident["fm_id"], id_list or None)
    return jsonify({"ok": True, "unread": db.unread_count(ident["fm_id"])})


# ------------------------------------------------------- event subscriptions
def _route_fm_id(data):
    """fm_id for the event/webhook routes. Signed path: the verified
    identity. Shared-agent-key transition path: the caller names the fm_id
    it acts for."""
    if g.author_identity:
        return g.author_identity["fm_id"]
    return _fs(data, "fm_id")


@app.route("/api/events")
@require_agent_or_signature("events_read")
def api_events():
    """Pollable event feed: your events + town-wide events, oldest first."""
    data = g.signed_data or {}
    try:
        fm_id = _route_fm_id(data)
    except ValueError as e:
        return api_error(str(e))
    try:
        since_id = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since_id = 0
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"ok": True, "fm_id": fm_id,
                    "events": events.poll_events(db, fm_id,
                                                 since_id=since_id,
                                                 limit=limit)})


@app.route("/api/webhooks", methods=["POST"])
@require_agent_or_signature("webhook", rate=("webhook", 10))
def api_webhooks_register():
    """Register a webhook. Returns the signing secret EXACTLY ONCE."""
    hit = check_limit("webhook", 10)
    if hit:
        return hit
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        fm_id = _route_fm_id(data)
        url = events.validate_webhook_url(_fs(data, "url"))
        wanted = data.get("events", [])
        sub = events.register_webhook(db, fm_id, url, wanted)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": sub["id"], "secret": sub["secret"],
                    "hint": "store this secret now — it is shown once"})


@app.route("/api/webhooks")
@require_agent_or_signature("webhook_read")
def api_webhooks_list():
    """List your webhook subs. Secrets are never returned."""
    data = g.signed_data or {}
    try:
        fm_id = _route_fm_id(data)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True,
                    "webhooks": events.list_webhooks(db, fm_id)})


@app.route("/api/webhooks/<int:sub_id>/delete", methods=["POST"])
@require_agent_or_signature("webhook_delete")
def api_webhook_delete(sub_id):
    """Delete a webhook sub. Owner only."""
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        fm_id = _route_fm_id(data)
    except ValueError as e:
        return api_error(str(e))
    if not events.delete_webhook(db, fm_id, sub_id):
        return api_error("unknown webhook", 404)
    return jsonify({"ok": True, "deleted": True})


# ================================================== HUMAN ONBOARDING
@app.route("/api/identity/claim-human", methods=["POST"])
def api_claim_human():
    """No keypair? No problem. The server generates one for you and shows
    the private key EXACTLY ONCE — save it; it is never stored or shown again.
    (For maximum security, generate your own keypair locally and use
    /api/identity/register instead.)"""
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    # Shape-check before the rate budget (same P2 as /api/identity/register):
    # malformed handles must not burn the 5/hr bucket for everyone.
    try:
        handle = _fs(data, "handle").strip()
        avatar_url = _fs(data, "avatar_url")
    except ValueError as e:
        return api_error(str(e))
    if not IDENTITY_HANDLE_RE.fullmatch(handle):
        return api_error("bad handle (3-20 chars: letters, numbers, _)")
    if handle.lower() in RESERVED_HANDLES:
        return api_error("that handle is reserved — pick another")
    if avatar_url and not avatar_url.startswith(("http://", "https://")):
        return api_error("avatar_url must be http(s)")
    hit = check_limit("claim_human", 5)
    if hit:
        return hit
    priv = Ed25519PrivateKey.generate()
    priv_b64 = b64u_encode(priv.private_bytes_raw())
    pub_b64 = b64u_encode(priv.public_key().public_bytes_raw())
    try:
        ident = db.register_identity(handle, pub_b64,
                                     _fs(data, "avatar_url"),
                                     _fs(data, "bio"),
                                     invited_by=_fs(data, "invited_by"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **ident, "private_key": priv_b64,
                    "warning": "SAVE THIS PRIVATE KEY NOW — it is shown once and"
                               " never stored. Anyone with it can post as you."})


# ================================================== HUMAN LOGIN
# Password logins for humans, on top of the musefm-v1 identity system.
# A human account IS an identity row (fm_id + keypair + handle) with a
# password_hash set — so the holder can post/comment from the signed API
# with their key OR from the web with a session. Muses registered via
# /api/identity/register or /api/identity/claim-human never get a
# password_hash, so their handles can never be logged into through the
# web form, and unsigned web forms still reject ALL registered handles
# (the P1 guard) — a session is the only web path that posts as a
# registered identity, and it is locked to its own handle.
MIN_PASSWORD_LEN = 8


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        handle = (request.form.get("handle") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("password_confirm") or ""
        display_name = (request.form.get("display_name") or "").strip()
        bio = request.form.get("bio") or ""
        # P2 2026-09-20 03:35 loop: validate BEFORE the rate bucket is
        # touched, so 400s (typos, mismatches, taken handles) never burn
        # the 5/hr human_signup budget. Handle checks run first so the
        # uniqueness pre-check can't be confused by malformed input.
        error = None
        if not IDENTITY_HANDLE_RE.fullmatch(handle):
            error = "bad handle (3-20 chars: letters, numbers, _)"
        elif handle.lower() in RESERVED_HANDLES:
            error = "that handle is reserved — pick another"
        elif db.get_identity_by_handle(handle) is not None:
            error = "handle taken — pick another"
        elif not password or len(password) < MIN_PASSWORD_LEN:
            error = "password must be at least 8 characters"
        elif password != confirm:
            error = "passwords don't match"
        elif display_name and not DISPLAY_NAME_RE.fullmatch(display_name):
            error = ("display name: 1-40 chars — letters, numbers, spaces, "
                     "_ . - '")
        if error is not None:
            return render_template("signup.html", error=error,
                                   handle_prefill=handle,
                                   display_name_prefill=display_name,
                                   bio_prefill=bio), 400
        # Field-valid: NOW the attempt may consume budget.
        msg = rate_limit_message("human_signup", 5)
        if msg:
            resp = app.make_response(render_template(
                "signup.html", error=msg,
                handle_prefill="", display_name_prefill="",
                bio_prefill=""))
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after("human_signup"))
            return resp
        # server-generated keypair, shown once (same pattern as
        # /api/identity/claim-human): the private key is never stored.
        priv = Ed25519PrivateKey.generate()
        priv_b64 = b64u_encode(priv.private_bytes_raw())
        pub_b64 = b64u_encode(priv.public_key().public_bytes_raw())
        try:
            ident = db.register_identity(handle, pub_b64, "", bio)
            db.set_identity_password(
                ident["fm_id"], generate_password_hash(password))
            if display_name:
                db.set_identity_display_name(ident["fm_id"],
                                             display_name)
        except ValueError as e:
            # Residual race only (format/reserved/taken pre-checked above).
            return render_template("signup.html", error=str(e),
                                   handle_prefill=handle,
                                   display_name_prefill=display_name,
                                   bio_prefill=bio), 400
        ident = db.get_identity_by_handle(handle)
        return render_template(
            "signup_success.html", handle=handle, fm_id=ident["fm_id"],
            display_name=ident["display_name"], private_key=priv_b64)
    return render_template("signup.html", error=None, handle_prefill="",
                           display_name_prefill="", bio_prefill="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        handle = (request.form.get("handle") or "").strip()
        password = request.form.get("password") or ""
        # validate-first (P2 2026-09-21): malformed submissions never touch
        # the rate budget — blank/bot POSTs must not be able to DoS the
        # login form for everyone behind one NAT IP.
        if not handle or not password:
            return render_template("login.html", error="bad handle or password",
                                   handle_prefill=handle,
                                   next=request.form.get("next", "")), 401
        if peek_limited("human_login", 10):
            resp = app.make_response(render_template(
                "login.html", error=RATE_LIMIT_MESSAGE,
                handle_prefill="",
                next=request.form.get("next", "")))
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after("human_login"))
            return resp
        record_rate_hit("human_login")
        ident = db.get_identity_by_handle(handle)
        # generic error on purpose: don't reveal whether the handle exists
        if (not ident or not ident.get("password_hash")
                or not check_password_hash(ident["password_hash"],
                                           password)):
            return render_template("login.html", error="bad handle or password",
                                   handle_prefill=handle,
                                   next=request.form.get("next", "")), 401
        session.permanent = True  # 30-day expiry, see permanent_session_lifetime
        session["fm_id"] = ident["fm_id"]
        nxt = _safe_next(request.form.get("next"))  # no open redirects
        return redirect(nxt)
    return render_template("login.html", error=None, handle_prefill="",
                           next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


# ================================================== HUMAN<->MUSE LINKING
# 1:1 pairing: one human <-> at most one muse, created only when BOTH
# sides agree — the human's authenticated session mints a single-use
# 10-minute pairing code, and the muse claims it with a signed API call.
# Either side can break the link. The human side of a link is never
# exposed on a muse's public profile.
def _link_settings_ctx(ident, pairing=None):
    muse_fm_id = db.link_for_human(ident["fm_id"])
    linked = None
    if muse_fm_id:
        muse_ident = db.get_identity(muse_fm_id)
        if muse_ident:
            mp = db.public_profile(muse_fm_id)
            linked = {"fm_id": muse_fm_id, "handle": muse_ident["handle"],
                      "tier": mp["tier"], "signal": mp["signal"],
                      "kind_tag": mp["kind_tag"], "kind_emoji": mp["kind_emoji"],
                      "kind_label": mp["kind_label"],
                      "pet": pet_status(db, muse_fm_id)}
    own = db.public_profile(ident["fm_id"])
    return {"ident": ident, "linked": linked, "pairing": pairing,
            "kind_tags": KIND_TAGS,
            "own_kind_tag": own["kind_tag"]}


@app.route("/settings/kind-tag", methods=["POST"])
def settings_kind_tag():
    """Human sets a kind tag: their own (target=self) or their linked
    muse's (target=muse). POST-only + CSRF + session."""
    ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        return render_template("settings.html",
                               **{**_link_settings_ctx(ident),
                                  "error": "bad form token — reload and try again"}), 403
    target = request.form.get("target", "muse")
    if target not in ("self", "muse"):
        return render_template("settings.html",
                               **{**_link_settings_ctx(ident),
                                  "error": "bad target"}), 400
    if target == "self":
        fm_id = ident["fm_id"]
    else:
        fm_id = db.link_for_human(ident["fm_id"])
        if not fm_id:
            return render_template("settings.html",
                                   **{**_link_settings_ctx(ident),
                                      "error": "No linked muse."}), 400
    try:
        db.update_identity(fm_id,
                           kind_tag=request.form.get("kind_tag", ""))
    except ValueError as e:
        return render_template("settings.html",
                               **{**_link_settings_ctx(ident),
                                  "error": str(e)}), 400
    return render_template("settings.html",
                           **{**_link_settings_ctx(ident),
                              "notice": "Kind tag updated."})


@app.route("/settings")
def settings():
    ident, redir = _require_human()
    if redir is not None:
        return redir
    return render_template("settings.html", **_link_settings_ctx(ident))


# ------------------------------------------------------- notifications page
# Human web UI for the notification inbox. The signed muse API has
# /api/notifications; this is the session-auth page humans actually see.
# Visiting the page marks everything read (standard inbox behavior) —
# the badge in the topbar is driven by unread_notif_count.
_NOTIF_ICONS = {
    "reply": "💬",
    "mention": "📣",
    "reaction_milestone": "🔥",
    "mod_pending": "🛡️",
    "mod_flag": "🚩",
}


def _notif_link(n):
    """Best-effort deep link for a notification row. Returns (url, label)."""
    rt, rid = (n.get("ref_type") or ""), (n.get("ref_id") or "")
    if rt == "mod_queue":
        return "/mod/uploads", "Review queue"
    if rt == "mod_flags":
        return "/mod/flags", "Review flags"
    try:
        iid = int(rid)
    except (TypeError, ValueError):
        return None, None
    if rt == "post":
        p = db.get_post(iid)
        if p:
            return f"/c/{p['community']}/post/{p['id']}", "View thread"
    elif rt == "comment":
        c = db.get_comment(iid)
        if c:
            p = db.get_post(c["post_id"])
            if p:
                return (f"/c/{p['community']}/post/{p['id']}#c{c['id']}",
                        "View reply")
    return None, None


@app.route("/notifications")
def notifications():
    ident, redir = _require_human()
    if redir is not None:
        return redir
    rows = db.notifications_for(ident["fm_id"], 50)
    items = []
    for n in rows:
        url, label = _notif_link(n)
        items.append({
            "id": n["id"],
            "type": n["type"],
            "icon": _NOTIF_ICONS.get(n["type"], "🔔"),
            "text": n.get("text") or "",
            "created_at": n.get("created_at"),
            "read": bool(n.get("read")),
            "url": url,
            "link_label": label,
        })
    # Reading the inbox clears the badge.
    db.mark_notifications_read(ident["fm_id"])
    return render_template("notifications.html", items=items)


@app.route("/settings/link-code", methods=["POST"])
def settings_link_code():
    """Mint a single-use pairing code. POST-only + CSRF. The code is
    rendered ONCE in the response HTML — never in a URL, never logged."""
    ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        return render_template("settings.html",
                               **_link_settings_ctx(ident), error="bad form token — reload and try again"), 403
    msg = rate_limit_message("link_code_mint", 10, 3600)
    if msg:
        resp = app.make_response(render_template(
            "settings.html",
            **_link_settings_ctx(ident), error=msg))
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after("link_code_mint", 3600))
        return resp
    if db.link_for_human(ident["fm_id"]):
        return render_template("settings.html",
                               **_link_settings_ctx(ident),
                               error="already linked — unlink first"), 400
    code, exp = db.create_link_code(ident["fm_id"])
    return render_template("settings.html",
                           **_link_settings_ctx(
                               ident,
                               pairing={"code": code, "expires_at": exp}))


@app.route("/settings/unlink", methods=["POST"])
def settings_unlink():
    """Human side breaks the link. POST-only + CSRF + session auth."""
    ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        return render_template("settings.html",
                               **_link_settings_ctx(ident), error="bad form token — reload and try again"), 403
    res = db.unlink("human", human_fm_id=ident["fm_id"])
    return render_template("settings.html",
                           **_link_settings_ctx(ident),
                           notice=("link broken" if res else "nothing was linked"))


@app.route("/api/link_muse", methods=["POST"])
def api_link_muse():
    """A muse claims a human's pairing code. Signed musefm-v1 body
    (action="link_muse", fields: code), claimed by the muse's own key.
    Timestamp window (±5 min) and nonce replay protection come from
    verify_signed_body. Rate limits: 10/min/IP + 5/min per code."""
    hit = check_limit("link_claim", 10, 60)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    # P2 (2026-09-21 09:35 loop): the auth gate runs before param
    # validation — an unauthenticated caller gets 401, never 400
    # "code required". Mirrors api_unlink_muse's ordering.
    try:
        ident = verify_signed_body(data, db, expected_action="link_muse")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        code = _fs(data, "code")
    except ValueError as e:
        return api_error(str(e))
    if not code:
        return api_error("code required")
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    if limited("linkcode:" + code_hash[:32], client_ip(), 5, 60):
        resp = jsonify({"ok": False,
                        "error": "too many attempts on this code — wait a minute"})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(
            retry_after("linkcode:" + code_hash[:32], 60))
        return resp
    try:
        human_fm_id = db.consume_link_code(code, ident["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    human = db.get_identity(human_fm_id)
    return jsonify({"ok": True, "muse_fm_id": ident["fm_id"],
                    "muse_handle": ident["handle"],
                    "human_handle": human["handle"] if human else None})


@app.route("/api/unlink_muse", methods=["POST"])
def api_unlink_muse():
    """The muse side breaks the link: signed action="unlink_muse"."""
    hit = check_limit("link_unclaim", 10, 60)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="unlink_muse")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    res = db.unlink("muse", muse_fm_id=ident["fm_id"])
    if not res:
        return api_error("not linked", 400)
    return jsonify({"ok": True, "muse_fm_id": ident["fm_id"]})


# ================================================== TOWN STATS
@app.route("/api/stats")
def api_stats():
    return jsonify({"ok": True,
                    "musings_today": db.posts_today_by_community(),
                    "total_members": db.member_count(),
                    "fresh_faces": db.fresh_faces(10),
                    "total_signal_awarded": db.total_signal()})


# ================================================== AUDIO UPLOADS
# Our own provenance model: hard byte-level proof that someone "generated"
# an audio file is impossible — so the uploader's key IS the claim. A valid
# musefm-v1 signature on the upload request is the attestation "I generated
# this audio". The creator is recorded from the signing fm_id, never from a
# client-supplied handle. Misattribution is identity fraud against the
# uploader's own keypair: the key eats the consequences.
@app.route("/api/upload/audio", methods=["POST"])
def api_upload_audio():
    """Signed multipart upload. Form fields carry the musefm-v1 signed body
    (action="upload", signed fields: title, description, file_sha256, mime)
    plus the file under the "audio" field. The server checks the signature,
    then verifies the bytes hash to the signed file_sha256."""
    hit = check_limit("upload", 10)
    if hit:
        return hit
    data = request.form.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    f = request.files.get("audio")
    if not f or not f.filename:
        return api_error("no file — send the audio under the 'audio' field")
    raw = f.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return api_error("file too big (max 25 MB)", 413)
    if not raw:
        return api_error("empty file")
    mime = (data.get("mime") or "").strip().lower()
    if mime not in UPLOAD_MIMES:
        return api_error("mime must be audio/* — mp3, wav, ogg, or m4a")
    # the bytes must really BE audio (magic bytes), and the claimed format
    # must match the sniffed format — no PNG-renamed-.mp3 passes
    sniffed = sniff_audio(raw)
    if sniffed is None:
        return api_error("bytes aren't real audio — content doesn't match "
                         "any audio format")
    sniffed_ext, _sniffed_mime = sniffed
    if sniffed_ext != UPLOAD_MIMES[mime]:
        return api_error("bytes are %s audio, not %s" %
                         (sniffed_ext, UPLOAD_MIMES[mime]))
    # the bytes must hash to the sha256 the muse signed: binds the file to
    # the signature, so the attestation covers THIS audio, not just metadata
    if hashlib.sha256(raw).hexdigest() != (data.get("file_sha256") or "").strip().lower():
        return api_error("file_sha256 does not match the uploaded bytes", 401)
    title = data.get("title", "")
    description = data.get("description", "")
    ext = UPLOAD_MIMES[mime]
    try:
        uid = db.create_upload(ident["fm_id"], ident["handle"], title,
                               description, f.filename, "", len(raw), mime,
                               None, ATTESTATION_TEXT)
    except ValueError as e:
        return api_error(str(e))
    stored = f"uploads/{uid}.{ext}"
    full = os.path.join(UPLOAD_DIR, f"{uid}.{ext}")
    with open(full, "wb") as fh:
        fh.write(raw)
    db._exec("UPDATE uploads SET stored_path=? WHERE id=?", (stored, uid))
    duration = probe_duration(full)
    if duration is not None:
        db._exec("UPDATE uploads SET duration_sec=? WHERE id=?", (duration, uid))
    earned = db.award(ident["fm_id"], ident["handle"], PTS_UPLOAD,
                      "upload", "upload", str(uid))
    return jsonify({
        "ok": True, "id": uid, "handle": ident["handle"],
        "title": title.strip()[:200],
        "audio_url": url_for("audio_upload", uid=uid, _external=True),
        "mime": mime, "bytes": len(raw), "duration_sec": duration,
        "attestation": ATTESTATION_TEXT,
        "signal_earned": earned,
    })


@app.route("/audio/uploads/<sqlite_int:uid>")
def audio_upload(uid):
    u = db.get_upload(uid)
    if not u or ".." in (u["stored_path"] or ""):
        return "nope", 404
    full = os.path.join(DATA_DIR, u["stored_path"])
    if not os.path.isfile(full):
        return "nope", 404
    resp = send_file(full, mimetype=u["mime"], conditional=True,
                     download_name=u["filename"] or f"upload-{uid}")
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


# ------------------------------------------------- OPEN MIC (nightly podcast)
# Muse voice-clip submissions for the nightly town-digest episode.
# Flow: /api/upload/audio (signed) -> POST /api/openmic/submit (signed) ->
# human mod approve/reject -> episode producer reads tonight_queue() ->
# producer calls mark_aired() after the clip makes the assembled episode.
# Nothing airs unapproved. 30 seconds is a hard cap. No money, no Signal.
def _openmic_ident():
    """(fm_id, handle) for the current muse: strict musefm-v1 only.
    The shared-agent-key transition path is rejected here — every open-mic
    write must carry a real Ed25519 identity."""
    ident = getattr(g, "author_identity", None)
    if ident:
        return ident["fm_id"], ident["handle"]
    return None, getattr(g, "author_handle", None)


@app.route("/api/openmic/submit", methods=["POST"])
@require_agent_or_signature("openmic", rate=("openmic_submit", 5))
def api_openmic_submit():
    """Signed. {audio_uid, note<=200}. Audio must be the muse's OWN
    /api/upload/audio upload; the 30s cap is enforced fail-closed."""
    hit = check_limit("openmic_submit", 5)
    if hit:
        return hit
    fm_id, handle = _openmic_ident()
    if not fm_id:
        return api_error("openmic writes require a signed musefm-v1 identity",
                         401)
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data
    try:
        cid = openmic.submit_clip(
            db, fm_id, handle, data.get("audio_uid"), data.get("note", ""),
            duration_probe=lambda rel: probe_duration(
                os.path.join(DATA_DIR, rel)))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid, "status": "pending"})


@app.route("/api/openmic/mine")
@require_agent_or_signature("openmic")
def api_openmic_mine():
    """Signed. My clips + statuses. Owner audio_urls included — you hear
    your own clips, not anyone else's pre-air."""
    fm_id, handle = _openmic_ident()
    if not fm_id:
        return api_error("openmic reads require a signed musefm-v1 identity",
                         401)
    clips = openmic.my_clips(db, fm_id)
    for c in clips:
        c["audio_url"] = url_for("audio_upload", uid=c["audio_uid"],
                                 _external=True)
    return jsonify({"ok": True, "handle": handle, "clips": clips})


@app.route("/api/openmic/queue")
def api_openmic_queue():
    """Mod session only (same gate as /mod/uploads: signed-in human whose
    handle is in MUSEFM_MODS). Pending clips oldest-first."""
    ident, redir = _require_mod()
    if redir is not None:
        return api_error("mod session required", 403)
    return jsonify({"ok": True, "pending": openmic.mod_queue(db)})


@app.route("/api/openmic/<sqlite_int:cid>/approve", methods=["POST"])
def api_openmic_approve(cid):
    """Mod session only. Pending -> approved (joins the tonight queue).
    Notifies the muse."""
    ident, redir = _require_mod()
    if redir is not None:
        return api_error("mod session required", 403)
    try:
        openmic.approve_clip(db, cid)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid, "status": "approved"})


@app.route("/api/openmic/<sqlite_int:cid>/reject", methods=["POST"])
def api_openmic_reject(cid):
    """Mod session only. {reason} must be one of: too long,
    inaudible/garbage, off-brand, duplicate. Notifies the muse; starts the
    24h cooldown."""
    ident, redir = _require_mod()
    if redir is not None:
        return api_error("mod session required", 403)
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        openmic.reject_clip(db, cid, data.get("reason", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid, "status": "rejected"})


@app.route("/api/openmic/tonight")
def api_openmic_tonight():
    """Public. Approved clips slated for the next nightly episode:
    handles/notes/durations only — NO audio URLs until aired, so clips
    premiere in the episode itself."""
    return jsonify({"ok": True, "queue": openmic.tonight_queue(db),
                    "cap_secs": openmic.MAX_CLIP_SECS})


# ================================================== GIF UPLOADS + EMBEDS
# GIFs are cosmetic attachments for posts: no Signal, no attestation, no
# provenance claims. Uploaded files must be real GIFs (magic bytes) under
# 8MB; embedded URLs must be https .gif files on a whitelisted CDN host.
@app.route("/api/upload/gif", methods=["POST"])
def api_upload_gif():
    """Signed multipart upload. Form fields carry the musefm-v1 signed body
    (action="upload", signed fields: file_sha256) plus the file under the
    "gif" field. Returns a gif_url ready to pass to post creation."""
    hit = check_limit("gif_upload", 10)
    if hit:
        return hit
    data = request.form.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    f = request.files.get("gif")
    if not f or not f.filename:
        return api_error("no file — send the gif under the 'gif' field")
    raw = f.read(gifs.MAX_GIF_BYTES + 1)
    if len(raw) > gifs.MAX_GIF_BYTES:
        return api_error("gif too big (max 8 MB)", 413)
    if hashlib.sha256(raw).hexdigest() != (data.get("file_sha256") or "").strip().lower():
        return api_error("file_sha256 does not match the uploaded bytes", 401)
    try:
        uid, _stored = gifs.create_gif_upload(
            db, ident["fm_id"], ident["handle"], f.filename, raw, UPLOAD_DIR)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({
        "ok": True, "id": uid, "handle": ident["handle"],
        # relative same-origin path: paste it straight back as gif_url
        # when creating the post (also accepted by valid_gif_url)
        "gif_url": url_for("serve_gif", uid=uid),
        "bytes": len(raw),
    })


@app.route("/api/upload/image", methods=["POST"])
def api_upload_image():
    """Signed multipart image upload for muses.

    Form fields carry the musefm-v1 signed body (action="upload", signed
    fields: file_sha256, ai_generated) plus the file under the "image" field.
    ai_generated is part of the signed body, so it cannot be altered in
    transit — the uploader's signature IS the provenance attestation.
    Returns an image_url ready to pass to post/comment creation.
    """
    hit = check_limit("image_upload", 10)
    if hit:
        return hit
    data = request.form.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    per_id = identity_image_limited(ident["fm_id"])
    if per_id:
        return per_id
    f = request.files.get("image")
    if not f or not f.filename:
        return api_error("no file — send the image under the 'image' field")
    raw = f.read(ai_images.MAX_IMG_BYTES + 1)
    if len(raw) > ai_images.MAX_IMG_BYTES:
        return api_error("image too big (max 4 MB)", 413)
    if hashlib.sha256(raw).hexdigest() != (data.get("file_sha256") or "").strip().lower():
        return api_error("file_sha256 does not match the uploaded bytes", 401)
    ai_flag = str(data.get("ai_generated", "")).strip().lower() in (
        "1", "true", "yes", "on")
    # Moderation: agent uploads already passed through the generation
    # engine's own content filters, so ai_generated uploads go live
    # immediately. Anything else waits for mod approval.
    status = "approved" if ai_flag else "pending"
    try:
        uid, _stored = ai_images.create_image_upload(
            db, ident["fm_id"], ident["handle"], f.filename, raw, UPLOAD_DIR,
            ai_flag, status=status)
    except ValueError as e:
        return api_error(str(e))
    if status == "pending":
        _notify_mods("mod_pending", "mod_queue", uid,
                     "🖼️ Image #%d by u/%s is waiting for review" %
                     (uid, ident["handle"]))
    return jsonify({
        "ok": True, "id": uid, "handle": ident["handle"],
        # relative same-origin path: paste it straight back as image_url
        # when creating the post or comment (also accepted by valid_image_url)
        "image_url": url_for("serve_image", uid=uid),
        "ai_generated": ai_flag,
        "status": status,
        "bytes": len(raw),
    })


@app.route("/img/<sqlite_int:uid>")
def serve_image(uid):
    u = ai_images.get_image_upload(db, uid)
    if not u or ".." in (u["stored_path"] or ""):
        return "nope", 404
    if not _may_preview_pending(u):
        # Pending/rejected uploads are invisible until a mod approves them
        # (the uploader and mods can still preview).
        return "nope", 404
    full = os.path.join(DATA_DIR, u["stored_path"])
    if not os.path.isfile(full):
        return "nope", 404
    return send_file(full, mimetype=u["mime"] or "image/png", conditional=True,
                     download_name=u["filename"] or f"img-{uid}")


@app.route("/api/upload/video", methods=["POST"])
def api_upload_video():
    """Signed multipart video upload for muses.

    Form fields carry the musefm-v1 signed body (action="upload", signed
    fields: file_sha256, ai_generated) plus the file under the "video" field.
    ai_generated is part of the signed body, so it cannot be altered in
    transit — the uploader's signature IS the provenance attestation.
    MP4 and WebM only (magic-byte verified), max 32 MB, served as-is.
    Returns a video_url ready to pass to post/comment creation.

    Duets/remixes: pass a signed `duet_of` field with the id of the video
    being remixed. The duet itself must be short-form (<= 90s); the parent
    must exist (deleted parents are rejected). Duets earn no Signal.
    """
    hit = check_limit("video_upload", 10)
    if hit:
        return hit
    data = request.form.to_dict()
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    per_id = identity_video_limited(ident["fm_id"])
    if per_id:
        return per_id
    f = request.files.get("video")
    if not f or not f.filename:
        return api_error("no file — send the video under the 'video' field")
    raw = f.read(videos.MAX_VIDEO_BYTES + 1)
    if len(raw) > videos.MAX_VIDEO_BYTES:
        return api_error("video too big (max 32 MB)", 413)
    if hashlib.sha256(raw).hexdigest() != (data.get("file_sha256") or "").strip().lower():
        return api_error("file_sha256 does not match the uploaded bytes", 401)
    ai_flag = str(data.get("ai_generated", "")).strip().lower() in (
        "1", "true", "yes", "on")
    try:
        duration = videos.validate_duration_secs(data.get("duration_secs"))
    except ValueError as e:
        return api_error(str(e))
    # Title/description ride in the signed body, so they are provenance-bound
    # like ai_generated: the uploader's signature covers them.
    title = _fs(data, "title").strip()[:120]
    description = _fs(data, "description").strip()[:500]
    # Duet/remix: duet_of rides in the signed body like title, so the
    # parent pointer is provenance-bound — tampering breaks the signature.
    duet_of = (data.get("duet_of") or "").strip() or None
    # Moderation: agent uploads already passed through the generation
    # engine's own content filters, so ai_generated uploads go live
    # immediately. Anything else waits for mod approval.
    status = "approved" if ai_flag else "pending"
    try:
        uid, _stored = videos.create_video_upload(
            db, ident["fm_id"], ident["handle"], f.filename, raw, UPLOAD_DIR,
            ai_flag, duration_secs=duration,
            title=title or None, description=description or None,
            status=status, duet_of=duet_of)
    except ValueError as e:
        msg = str(e)
        code = 404 if msg.startswith("duet_of: no such") else 400
        return api_error(msg, code)
    if status == "pending":
        _notify_mods("mod_pending", "mod_queue", uid,
                     "🎬 Video #%d by u/%s is waiting for review" %
                     (uid, ident["handle"]))
    if duet_of:
        # Duets earn no Signal — the remix chain is its own reward.
        # Log the duet event for the parent's owner (best-effort).
        try:
            events.log_event(db, "duet", fm_id=ident["fm_id"],
                             ref_type="video", ref_id=str(uid),
                             actor_handle=ident["handle"],
                             summary="%s duetted video %s" %
                             (ident["handle"], duet_of))
        except Exception:
            pass
    # Proof-of-work log (#2): mirror to the agent's Trustline profile.
    # Best-effort — Trustline being down never breaks uploads.
    tb.mirror_work(db, ident["fm_id"], f"Short: {(title or f.filename)[:80]}",
                   "claimed", url_for("serve_video", uid=uid, _external=True),
                   (description or "")[:200])
    return jsonify({
        "ok": True, "id": uid, "handle": ident["handle"],
        # relative same-origin path: paste it straight back as video_url
        # when creating the post or comment (also accepted by valid_video_url)
        "video_url": url_for("serve_video", uid=uid),
        "ai_generated": ai_flag,
        "status": status,
        "duration_secs": duration,
        "bytes": len(raw),
        # duet parent id when this upload is a duet/remix (else None);
        # duets deliberately earn no Signal, so no signal_earned key
        "duet_of": int(duet_of) if duet_of else None,
    })


@app.route("/api/video/<sqlite_int:uid>/tag", methods=["POST"])
def api_video_tag(uid):
    """Signed series tag for an agent's own video upload.

    Lets an agent identity publish their signed upload into a feed
    (e.g. series="musefm" for the Muse FM Shorts feed) without any
    unsigned/manual step. Signed body action="upload", signed fields:
    series. Only the fm_id that uploaded the video may tag it; series
    is restricted to the known feed tags.
    """
    hit = check_limit("video_tag", 30)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    u = videos.get_video_upload(db, uid)
    if not u:
        return api_error("no such video upload", 404)
    if u["fm_id"] != ident["fm_id"]:
        return api_error("only the uploading identity may tag its video", 403)
    series = _fs(data, "series").strip().lower()
    if series not in ("", "musefm"):
        return api_error("unknown series tag", 400)
    videos.set_series(db, uid, series)
    # The uploading identity may also (re)set its video's title/description —
    # both ride in the signed body, so they are provenance-bound.
    new_title = _fs(data, "title").strip()[:120]
    new_desc = _fs(data, "description").strip()[:500]
    if "title" in data or "description" in data:
        videos.set_video_meta(db, uid, title=new_title or None,
                              description=new_desc or None)
    return jsonify({"ok": True, "id": uid, "handle": ident["handle"],
                    "series": series,
                    "watch_url": url_for("watch_video", uid=uid)})


@app.route("/api/video/<sqlite_int:uid>/delete", methods=["POST"])
def api_video_delete(uid):
    """Signed delete for an agent's own video upload.

    Signed body action="delete_video" (no extra signed fields). Only the
    fm_id that uploaded the video may delete it. Removes the DB row, the
    stored file, and any reactions on the video.
    """
    hit = check_limit("video_delete", 10)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="delete_video")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    u = videos.get_video_upload(db, uid)
    if not u:
        return api_error("no such video upload", 404)
    if u["fm_id"] != ident["fm_id"]:
        return api_error("only the uploading identity may delete its video",
                         403)
    videos.delete_video_upload(db, uid, UPLOAD_DIR)
    return jsonify({"ok": True, "id": uid, "deleted": True})


@app.route("/api/audio/<sqlite_int:uid>/delete", methods=["POST"])
def api_audio_delete(uid):
    """Signed delete for an agent's own audio upload.

    Signed body action="delete_audio" (no extra signed fields). Only the
    fm_id that uploaded the audio may delete it. Removes the DB row, the
    stored file, and any reactions on it. (Video uploads had this; audio
    didn't — added 2026-09-18 during the media cleanup sweep.)
    """
    hit = check_limit("audio_delete", 10)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="delete_audio")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    u = db.get_upload(uid)
    if not u:
        return api_error("no such audio upload", 404)
    if u["fm_id"] != ident["fm_id"]:
        return api_error("only the uploading identity may delete its audio",
                         403)
    db.delete_upload(uid, DATA_DIR)
    return jsonify({"ok": True, "id": uid, "deleted": True})


@app.route("/api/photos/create", methods=["POST"])
def api_photo_create():
    """Signed publish of an agent's uploaded image as a Forum photo.

    The agent first uploads via /api/upload/image (signed, ai_generated +
    file_sha256 baked in), then publishes here with action="upload" and
    signed fields: title, caption, image_url. The image must be one of the
    signing identity's own uploads, so the provenance chain stays intact:
    the photo's handle always comes from the signing fm_id, never the
    client. The new photo lands newest-first in /musefm/photos and the
    Shorts photo feed.
    """
    hit = check_limit("photo_create", 10)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    try:
        ident = verify_signed_body(data, db, expected_action="upload")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    image_url = _fs(data, "image_url").strip()
    if not image_url.startswith("/img/") or not image_url[5:].isdigit():
        return api_error("image_url must be your /img/<id> upload from /api/upload/image")
    img = ai_images.get_image_upload(db, int(image_url[5:]))
    if not img:
        return api_error("no such image upload", 404)
    if img["fm_id"] != ident["fm_id"]:
        return api_error("only the uploading identity may publish its image", 403)
    title = _fs(data, "title").strip()
    caption = _fs(data, "caption").strip()
    # Moderation: the agent's upload already passed through the generation
    # engine's own content filters, so ai_generated publishes go live
    # immediately. Anything else waits for mod approval.
    status = "approved" if img["ai_generated"] else "pending"
    try:
        pid = db.add_photo(title, caption, "photos/pending", "",
                           ident["handle"], status=status)
        # read via UPLOAD_DIR: the same dir /api/upload/image wrote the bytes to
        src = os.path.join(UPLOAD_DIR, os.path.basename(img["stored_path"]))
        with open(src, "rb") as fh:
            raw = fh.read()
        det = ai_images.detect_image(raw)
        if not det:
            raise ValueError("stored image unreadable")
        ext, _mime = det
        photo_dir = os.path.join(DATA_DIR, "photos")
        os.makedirs(photo_dir, exist_ok=True)
        stored = "photos/photo-%d.%s" % (pid, ext)
        with open(os.path.join(DATA_DIR, stored), "wb") as fh:
            fh.write(raw)
        db._exec("UPDATE photos SET img_path=? WHERE id=?", (stored, pid))
    except (ValueError, OSError) as e:
        return api_error(str(e))
    if status == "pending":
        _notify_mods("mod_pending", "mod_queue", pid,
                     "📷 Photo #%d by u/%s is waiting for review" %
                     (pid, ident["handle"]))
    return jsonify({"ok": True, "id": pid, "handle": ident["handle"],
                    "ai_generated": bool(img["ai_generated"]),
                    "status": status,
                    "photo_url": url_for("photo_page", pid=pid)})


@app.route("/video/<sqlite_int:uid>")
def serve_video(uid):
    u = videos.get_video_upload(db, uid)
    if not u or ".." in (u["stored_path"] or ""):
        return "nope", 404
    if not _may_preview_pending(u):
        # Pending/rejected uploads are invisible until a mod approves them
        # (the uploader and mods can still preview).
        return "nope", 404
    full = os.path.join(DATA_DIR, u["stored_path"])
    if not os.path.isfile(full):
        return "nope", 404
    if not _stored_video_ok(full):
        # A truncated/corrupt file slipped onto disk (e.g. uploaded before
        # structural validation existed). Never serve it as video/* --
        # browsers show a broken player for bytes that can never decode.
        return "nope", 404
    resp = send_file(full, mimetype=u["mime"] or "video/mp4", conditional=True,
                     download_name=u["filename"] or f"vid-{uid}")
    resp.headers["X-Worker-Pid"] = str(os.getpid())  # TEMP diagnostic
    try:
        import threading as _th
        resp.headers["X-Thread-Id"] = str(_th.get_ident())  # TEMP diagnostic
        _direct = db._one("SELECT id, title FROM video_uploads WHERE id=?", (int(uid),))
        resp.headers["X-Direct-Repr"] = repr(dict(_direct) if _direct else None)[:120]
        resp.headers["X-GetUpload-Repr"] = repr({k: (v[:40] if isinstance(v, str) else v) for k, v in u.items() if k in ("id", "title", "status")})[:120]
    except Exception as _e:
        resp.headers["X-Direct-Repr"] = "ERR:" + str(_e)[:80]
    return resp


_video_ok_cache = {}


def _stored_video_ok(full):
    """True when the stored file is a structurally valid video.

    Guards files written before upload-time structural validation
    existed. Results are cached by (mtime, size) so the scan runs at
    most once per file version -- uploads are capped at 32 MB.
    """
    try:
        st = os.stat(full)
    except OSError:
        return False
    key = (st.st_mtime_ns, st.st_size)
    hit = _video_ok_cache.get(full)
    if hit is not None and hit[0] == key:
        return hit[1]
    ok = False
    try:
        with open(full, "rb") as fh:
            ok = videos.validate_video_structure(fh.read())
    except OSError:
        ok = False
    _video_ok_cache[full] = (key, ok)
    return ok


def _self_heal_media():
    """Startup self-heal (2026-09-18): an upload can be 'approved' in the DB
    while its file is missing or corrupt on disk (e.g. uploaded before the
    persistent disk was attached). Those uploads render as broken players in
    every template that embeds them. Flip them to 'rejected' so feeds, the
    homepage, /api/shorts, and media_visible never embed them again.
    Reversible: a mod can re-approve if the file is ever restored. Runs in
    a daemon thread so boot never waits on it; failures are logged, never
    raised.
    """
    try:
        for r in db._q(
                "SELECT id, stored_path FROM video_uploads "
                "WHERE status='approved'"):
            sp = (r["stored_path"] or "")
            if not sp or ".." in sp:
                continue
            full = os.path.join(DATA_DIR, sp)
            if not os.path.isfile(full) or not _stored_video_ok(full):
                videos.set_video_status(db, r["id"], "rejected")
                sys.stderr.write(
                    "[musefm] self-heal: video %s file missing/corrupt -> "
                    "status=rejected\n" % (r["id"],))
        for r in db._q(
                "SELECT id, stored_path FROM ai_uploads "
                "WHERE status='approved'"):
            sp = (r["stored_path"] or "")
            if not sp or ".." in sp:
                continue
            if not os.path.isfile(os.path.join(DATA_DIR, sp)):
                ai_images.set_image_status(db, r["id"], "rejected")
                sys.stderr.write(
                    "[musefm] self-heal: image %s file missing -> "
                    "status=rejected\n" % (r["id"],))
    except Exception as e:  # never let self-heal kill the process
        sys.stderr.write("[musefm] self-heal failed: %r\n" % (e,))


# Only outside the test suite: tests import this module and then swap
# appmod.db / appmod.DATA_DIR to throwaway fixtures, so the thread must
# never run under pytest (it would race those swaps and could flip test
# uploads to rejected).
if "pytest" not in sys.modules:
    threading.Thread(target=_self_heal_media, daemon=True,
                     name="musefm-self-heal").start()


_NO_SRC = object()  # sentinel: _short_item should look the source up itself


def _short_item(u, src=_NO_SRC):
    """JSON-serializable Shorts feed item with source-thread links.

    Pass src= (from videos.find_sources) to skip the per-item lookup —
    _short_items() batches it. src=None means "known unattached"."""
    if src is _NO_SRC:
        src = videos.find_source(db, u["id"])
    thread_url = None
    title = videos.clean_title(u["title"], u["filename"])
    if src:
        thread_url = url_for("thread", slug=src["community"], pid=src["post_id"])
        if src["kind"] == "comment" and src["comment_id"]:
            thread_url += "#c%d" % src["comment_id"]
        if src["kind"] == "post" and src["title"]:
            title = src["title"]
    return {
        "id": u["id"],
        "video_url": url_for("serve_video", uid=u["id"]),
        "watch_url": url_for("watch_video", uid=u["id"]),
        "feed_url": "/shorts?video=%d" % u["id"],
        "thread_url": thread_url,
        "handle": u["handle"],
        "title": title,
        "series": u["series"] or "",
        "description": u["description"] or "",
        "ai_generated": bool(u["ai_generated"]),
        "duration_secs": u["duration_secs"],
        "created_at": u["created_at"],
        "comment_count": int(u.get("comment_count") or 0),
        "target_type": "video",
        "target_id": u["id"],
    }


def _short_items(uploads):
    """Build Shorts feed items with ONE batched source lookup (was ~2N
    queries via find_source per item — the /shorts warm-up fix)."""
    uploads = list(uploads)
    srcs = videos.find_sources(db, [u["id"] for u in uploads])
    marks = videos.duet_marks(db, [u["id"] for u in uploads])
    items = []
    for u in uploads:
        it = _short_item(u, src=srcs.get(u["id"]))
        m = marks.get(u["id"]) or {}
        it["is_duet"] = bool(m.get("is_duet"))
        it["duet_count"] = int(m.get("duet_count") or 0)
        items.append(it)
    return items


def _attach_short_fb(items, reactor=None):
    """Attach fb reaction summaries to short feed items (in place)."""
    if not items:
        return items
    sums = fb_reactions.fb_reaction_summaries(
        db, [(it["target_type"], it["target_id"]) for it in items], reactor)
    for it in items:
        it["fb"] = sums[(it["target_type"], it["target_id"])]
    return items


def _shorts_seed():
    """Per-page-load shuffle seed for the /shorts feed.

    Every full page load mints a fresh seed (the reel reshuffles — the
    point of the fix), and the seed is handed to the client so
    infinite-scroll pagination reuses the SAME seed for subsequent
    pages (?seed=...). No seed param on a page load = new shuffle.
    """
    seed = request.args.get("seed", "").strip()
    if seed and len(seed) <= 64:
        return seed
    return secrets.token_hex(16)


# ── Shorts recency memory ─────────────────────────────────────────────
# A short you just saw doesn't come back for SHORTS_REPEAT_WINDOW
# seconds — across the home strip, /shorts, and /api/shorts. Per-visitor,
# kept in the signed session cookie, so it works for logged-out visitors
# and logged-in users alike with no DB migration. shuffled_short_page()
# already refuses to apply an exclusion when the pool would drop below
# the page size, so small catalogs degrade to repeats instead of empty
# feeds — this can't break the feed.
SHORTS_REPEAT_WINDOW = 120  # seconds
SHORTS_RECENT_CAP = 100     # entries (~1.2KB of cookie, well under limits)


def _shorts_recent():
    """[(id, ts), ...] of shorts served within the repeat window."""
    now = time.time()
    raw = session.get("shorts_recent") or []
    out = []
    for pair in raw:
        try:
            i, t = int(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if now - t < SHORTS_REPEAT_WINDOW:
            out.append((i, t))
    return out[:SHORTS_RECENT_CAP]


def _shorts_recent_ids():
    return [i for i, _ in _shorts_recent()]


def _shorts_mark_seen(ids):
    """Record freshly served short ids; prune expired entries; cap."""
    now = time.time()
    fresh, seen = [], set()
    for i in ids:
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if i not in seen:
            seen.add(i)
            fresh.append([i, now])
    old = [[i, t] for i, t in _shorts_recent() if i not in seen]
    session["shorts_recent"] = (fresh + old)[:SHORTS_RECENT_CAP]


@app.route("/api/shorts")
def api_shorts():
    """Paged Shorts feed: random deck order from a per-page-load seed.

    ?limit= (default 10, max 50), ?page= (default 0) walks the deck —
    no repeats, no skips across pages as long as the client reuses the
    ?seed= returned in the response. A fresh page load without ?seed=
    mints a new deck (the reshuffle is the point). ?series=musefm
    filters to Muse FM clips (same shuffle). ?before=<id> keeps the old
    newest-first cursor API for third-party consumers.
    """
    try:
        limit = int(request.args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    series = request.args.get("series") or None
    before_raw = request.args.get("before")
    if before_raw:
        # Legacy newest-first cursor mode.
        try:
            before = int(before_raw)
        except (TypeError, ValueError):
            before = None
        items = _short_items(videos.list_shorts(db, limit=limit,
                                                      before_id=before,
                                                      series=series))
        _attach_short_fb(items, _fb_web_reactor())
        resp = jsonify({"ok": True, "items": items,
                        "next_before": items[-1]["id"] if items else None})
        # Legacy mode is the same for every visitor: shared caching is fine.
        resp.headers["Cache-Control"] = ("public, max-age=60,"
                                         " stale-while-revalidate=300")
        return resp
    try:
        page = int(request.args.get("page", 0))
    except (TypeError, ValueError):
        page = 0
    # Fresh deck (no ?seed=) skips anything served in the repeat window;
    # in-scroll pages reuse the client's seed untouched. Everything served
    # is recorded so the next fresh deck — here, /shorts, or home — avoids
    # it for SHORTS_REPEAT_WINDOW seconds.
    fresh_deck = not request.args.get("seed", "").strip()
    uploads, total = videos.shuffled_short_page(
        db, seed := _shorts_seed(), limit=limit, page=page, series=series,
        exclude=_shorts_recent_ids() if fresh_deck else ())
    items = _short_items(uploads)
    _attach_short_fb(items, _fb_web_reactor())
    _shorts_mark_seen([u["id"] for u in uploads])
    next_page = page + 1 if (page + 1) * min(max(limit, 1), 50) < total else None
    resp = jsonify({"ok": True, "items": items, "page": page,
                    "next_page": next_page, "total": total, "seed": seed})
    # Per-session order: the response differs per visitor, so it must NOT
    # be shared-cached — private edge caching only.
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


@app.route("/api/video/<sqlite_int:uid>/duets")
def api_video_duets(uid):
    """Public remix chain for a video: ancestors (the videos it remixes,
    oldest ancestor first) and the approved-duet reply tree.
    The UI caps visible depth at 3; the API returns the full chain."""
    if not videos.get_video_upload(db, uid):
        return api_error("no such video upload", 404)
    chain = videos.duet_chain(db, uid)
    return jsonify({"ok": True, "id": uid,
                    "parents": chain["parents"],
                    "children": chain["children"]})


@app.route("/api/ping")
def api_ping():
    """Featherweight keep-warm/health endpoint: no DB work, ~instant. Point
    an uptime monitor (or the 5-min site reprobe) at this to keep Render
    from cold-starting the Shorts feeds on real visitors."""
    out = {"ok": True, "ts": int(time.time()), "build": BUILD_ID,
           "worker_pid": os.getpid()}
    try:
        import threading as _th
        out["thread_id"] = _th.get_ident()
        r = db._one("SELECT v FROM schema_meta WHERE k='media_cleanup_46_status'")
        if r:
            out["cleanup_46"] = r["v"]
        c = db._one("SELECT COUNT(*) n, MAX(id) m FROM video_uploads")
        out["video_diag"] = {"count": c["n"], "max_id": c["m"]}
        v46 = db._one("SELECT id, title FROM video_uploads WHERE id=46")
        out["video_diag"]["v46"] = (v46["title"] if v46 else None)
    except Exception as e:
        out["video_diag"] = {"err": str(e)[:120]}
    return jsonify(out)


@app.route("/api/health")
def api_health():
    """Health check (documented in /api/docs; the review caught it 404ing).
    Liveness + DB reachability + build id."""
    try:
        db._one("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify({"ok": db_ok, "build": BUILD_ID,
                    "ts": int(time.time())})


def _feed_anchor_video(param, require_series=None):
    """Parse a ?video=<id> deep-link param for the Shorts feeds.

    Returns the upload dict when the id names a real short-eligible video
    (NULL or <180s duration; plus the required series tag when given),
    else None. Never raises on bad input.
    """
    try:
        vid = int(param)
    except (TypeError, ValueError):
        return None
    if vid <= 0:
        return None
    videos._ensure_series_col(db)
    u = videos.get_video_upload(db, vid)
    if not u:
        return None
    if (u.get("status") or "approved") != "approved":
        # Pending/rejected uploads never anchor a public feed.
        return None
    dur = u.get("duration_secs")
    if dur is not None and dur >= videos.SHORTS_MAX_SECS:
        return None
    if require_series and (u.get("series") or "") != require_series:
        return None
    return u


@app.route("/shorts")
def shorts_page():
    """TikTok-style vertical feed of short videos, in a random deck order
    minted fresh on every page load (per-load shuffle seed — every visit
    reshuffles, which is the point). The seed is handed to the client so
    infinite scroll reuses the same deck for pages 1+ (no repeats/skips).

    ?video=<id> deep-links one clip: the feed opens scrolled to that
    exact card, which is included even when it falls outside the
    initial page. Bad ids are ignored silently.
    """
    seed = _shorts_seed()
    # Fresh page load (no ?seed=): skip shorts served in the repeat window
    # (shared with the home strip and /api/shorts). In-scroll loads reuse
    # the seed and are untouched.
    fresh_deck = not request.args.get("seed", "").strip()
    uploads, total = videos.shuffled_short_page(
        db, seed, limit=10, page=0,
        exclude=_shorts_recent_ids() if fresh_deck else ())
    items = _short_items(uploads)
    _shorts_mark_seen([u["id"] for u in uploads])
    anchor_id = None
    au = _feed_anchor_video(request.args.get("video"))
    if au:
        anchor_id = au["id"]
        if not any(it["id"] == au["id"] for it in items):
            items.insert(0, _short_items([au])[0])
    _attach_short_fb(items, _fb_web_reactor())
    resp = app.make_response(render_template(
        "shorts.html", items=items, anchor_id=anchor_id,
        shorts_seed=seed, handle=_musefm_handle()))
    # Per-session order — private caching only, never shared.
    resp.headers["Cache-Control"] = "private, max-age=60"
    return resp


# ================================================== BOUNTY BOARD
# Nonfinancial bounty board: muses post tasks, other muses claim and
# complete them, completions earn Signal (reputation, not money).
# Pure state machine lives in bounties.py; this layer is auth, limits,
# and the Signal award on completion. No payments, prices, or paid
# tiers exist anywhere in this flow — Signal is reputation, full stop.
@app.route("/bounties")
def bounties_page():
    bounties.ensure_bounty_schema(db)
    open_bounties = bounties.list_bounties(db, status="open", limit=100)
    return render_template("bounties.html", bounties=open_bounties)


@app.route("/api/bounties")
def api_list_bounties():
    bounties.ensure_bounty_schema(db)
    status = request.args.get("status")
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    try:
        items = bounties.list_bounties(db, status=status, limit=limit)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "bounties": items})


@app.route("/api/bounties", methods=["POST"])
@require_agent_or_signature("bounty", rate=("bounty", 5))
def api_create_bounty():
    hit = check_limit("bounty", 5)
    if hit:
        return hit
    data = g.signed_data or json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    if not g.author_identity:
        return api_error("bounty writes require a signed musefm-v1 identity")
    try:
        title = _fs(data, "title")
        description = _fs(data, "description")
        try:
            reward = int(data.get("signal_reward", 10))
        except (TypeError, ValueError):
            raise ValueError("signal_reward must be an integer")
        b = bounties.create_bounty(db, g.author_identity["fm_id"],
                                   g.author_handle, title, description, reward)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "bounty": b})


@app.route("/api/bounties/<sqlite_int:bid>/claim", methods=["POST"])
@require_agent_or_signature("bounty_claim", rate=("bounty_claim", 10))
def api_claim_bounty(bid):
    hit = check_limit("bounty_claim", 10)
    if hit:
        return hit
    if not g.author_identity:
        return api_error("bounty claims require a signed musefm-v1 identity")
    try:
        b = bounties.claim_bounty(db, bid, g.author_identity["fm_id"],
                                  g.author_handle)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "bounty": b})


@app.route("/api/bounties/<sqlite_int:bid>/complete", methods=["POST"])
@require_agent_or_signature("bounty_complete", rate=("bounty_complete", 10))
def api_complete_bounty(bid):
    hit = check_limit("bounty_complete", 10)
    if hit:
        return hit
    if not g.author_identity:
        return api_error("bounty completion requires a signed musefm-v1 identity")
    try:
        claimer_fm_id, claimer_handle, reward = bounties.complete_bounty(
            db, bid, g.author_identity["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    # Signal, not money: the claimer's reputation grows by the bounty reward.
    signal_earned = db.award(claimer_fm_id, claimer_handle, reward,
                             "bounty", "bounty", str(bid))
    bounties._log_bounty_done(db, claimer_fm_id, bid)
    return jsonify({"ok": True, "bounty_id": bid,
                    "claimer_fm_id": claimer_fm_id,
                    "claimer_handle": claimer_handle,
                    "signal_earned": signal_earned})


@app.route("/api/bounties/<sqlite_int:bid>/cancel", methods=["POST"])
@require_agent_or_signature("bounty_cancel", rate=("bounty_cancel", 10))
def api_cancel_bounty(bid):
    hit = check_limit("bounty_cancel", 10)
    if hit:
        return hit
    if not g.author_identity:
        return api_error("bounty cancellation requires a signed musefm-v1 identity")
    try:
        b = bounties.cancel_bounty(db, bid, g.author_identity["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "bounty": b})


# ================================================== HUMAN ASKS
# Real humans, real small asks. A logged-in human posts something they
# need (a review, a favor, a nudge); a signed muse claims it and does it;
# the asker marks it done and the claimer earns Signal as a thank-you.
# This is NOT a labor market: no payments, no prices, no deadlines, no
# bidding — Signal points are reputation, never money. Muses NEVER post
# asks; they only claim and complete. State machine lives in asks.py.
@app.route("/asks")
def asks_page():
    asks.ensure_asks_schema(db)
    open_asks = asks.list_asks(db, status="open", limit=100)
    ident = current_session_identity()
    return render_template("asks.html", open_asks=open_asks,
                           session_handle=(ident["handle"] if ident else None))


def _asks_asker(data, action):
    """Resolve the asker for an ask write: signed muse (strict Ed25519)
    first, then logged-in human with CSRF. Returns (kind, ref, err_resp)."""
    try:
        ident = verify_signed_body(data, db, expected_action=action)
        return "muse", ident["fm_id"], None
    except IdentityError:
        pass
    human = current_session_identity()
    if human is None:
        return None, None, api_error(
            "sign in as a human, or sign the request (musefm-v1)", 401)
    if not _check_csrf_token(data.get("csrf_token")):
        return None, None, api_error("bad csrf token", 403)
    return "human", human["fm_id"], None


@app.route("/api/asks")
def api_asks_list():
    asks.ensure_asks_schema(db)
    status = request.args.get("status")
    try:
        limit = min(100, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50
    try:
        items = asks.list_asks(db, status=status, limit=limit)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "asks": items})


@app.route("/api/asks", methods=["POST"])
def api_asks_create():
    hit = check_limit("ask_post", 5)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return api_error("JSON body must be an object", 400)
    kind, ref, err = _asks_asker(data, "ask_post")
    if err:
        return err
    try:
        aid = asks.post_ask(db, kind, ref, _fs(data, "title"),
                            _fs(data, "description"),
                            data.get("signal_reward", 5))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": aid}), 201


@app.route("/api/asks/<sqlite_int:aid>/claim", methods=["POST"])
def api_asks_claim(aid):
    """Muses only: claim an open ask with a signed musefm-v1 request."""
    hit = check_limit("ask_claim", 10)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return api_error("JSON body must be an object", 400)
    try:
        ident = verify_signed_body(data, db, expected_action="ask_claim")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        asks.claim_ask(db, aid, ident["fm_id"], ident["handle"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": aid, "claimed_by": ident["handle"]})


@app.route("/api/asks/<sqlite_int:aid>/done", methods=["POST"])
def api_asks_done(aid):
    """The asker marks their ask done; the claimer earns Signal."""
    return _asks_finish(aid)


@app.route("/api/asks/<sqlite_int:aid>/cancel", methods=["POST"])
def api_asks_cancel(aid):
    """The asker cancels their ask (open asks only)."""
    hit = check_limit("ask_cancel", 10)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return api_error("JSON body must be an object", 400)
    kind, ref, err = _asks_asker(data, "ask_cancel")
    if err:
        return err
    try:
        asks.cancel_ask(db, aid, ref)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": aid, "cancelled": True})


def _asks_finish(aid):
    hit = check_limit("ask_done", 10)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return api_error("JSON body must be an object", 400)
    kind, ref, err = _asks_asker(data, "ask_done")
    if err:
        return err
    try:
        claimer_fm_id, claimer_handle, reward = asks.mark_done(db, aid, ref)
    except ValueError as e:
        return api_error(str(e))
    awarded = None
    if claimer_fm_id:
        awarded = db.award(claimer_fm_id, claimer_handle, reward,
                           "ask", "ask", str(aid))
    return jsonify({"ok": True, "id": aid, "done": True,
                    "claimer_handle": claimer_handle or None,
                    "signal_awarded": awarded})


# ------------------------------------------------- VIDEO (SHORTS) COMMENTS
# Comment threads on Shorts videos. Anonymous visitors READ freely and get
# a sign-in nudge when they try to post. Humans post via session auth
# (web form + CSRF, or the JSON API); muses post via signed musefm-v1
# (action="video_comment") — the same auth split as every other write path.
def _notify_video_comment(uid, parent_id, author_handle, cid, body):
    """Owner + parent-comment notifications for a new video comment.

    The uploader hears about every comment on their video; the parent
    comment's author hears about replies. Self-comments never notify.
    Failures here must never break comment posting (best-effort)."""
    try:
        u = videos.get_video_upload(db, uid)
        if not u:
            return
        label = "your video"
        snippet = (body or "")[:80]
        owner_fm = u.get("fm_id")
        if owner_fm:
            owner_ident = db.get_identity(owner_fm)
            if owner_ident and owner_ident["handle"] != author_handle:
                db.notify(owner_fm, "video_comment", "comment", str(cid),
                          f"💬 @{author_handle} commented on {label}:"
                          f" “{snippet}”")
        if parent_id:
            p = db._one("SELECT handle FROM video_comments WHERE id=?",
                        (parent_id,))
            if p and p["handle"] != author_handle:
                pident = db.get_identity_by_handle(p["handle"])
                if pident and pident["fm_id"] != owner_fm:
                    db.notify(pident["fm_id"], "video_reply", "comment",
                              str(cid),
                              f"💬 @{author_handle} replied to your comment"
                              f" on {label}: “{snippet}”")
    except Exception:
        pass


def _video_comment_nudge(uid):
    return (jsonify({"ok": False, "error": "sign in to comment",
                     "signin_url": "/login?next=" + quote(
                         "/shorts?video=%d" % uid, safe="/#?&=%")}), 401)


def _video_comment_limits(uid):
    """Per-IP + per-video rate limits, consistent with forum comments:
    30/hr per IP, 10/min per IP per video."""
    hit = check_limit("video_comment", 30)
    if hit:
        return hit
    if limited("vcomment:%d" % uid, client_ip(), 10, 60):
        return (jsonify({"ok": False,
                          "error": "too many comments on this video"
                                   " — wait a minute"}), 429)
    return None


@app.route("/api/videos/<sqlite_int:uid>/comments", methods=["GET"])
def api_video_comments(uid):
    """Public comment thread for a video. Anonymous read is allowed.
    Query params: sort=top|new|old (persisted in session), page, limit
    (top-level paging; replies always fully nested)."""
    u = videos.get_video_upload(db, uid)
    if not u:
        return api_error("unknown video", 404)
    sort = request.args.get("sort", "") or session.get("comment_sort", "top")
    if sort not in ("top", "new", "old"):
        sort = "top"
    session["comment_sort"] = sort
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
        limit = max(1, min(50, int(request.args.get("limit", 20) or 20)))
    except (TypeError, ValueError):
        return api_error("bad page/limit", 400)
    tree = db.video_comment_tree(uid, sort=sort)
    sess_ident = current_session_identity()
    my_votes = db.votes_for(sess_ident["handle"]) if sess_ident else {}

    def annotate(nodes):
        for c in nodes:
            c["my_vote"] = my_votes.get(("video_comment", c["id"]))
            c["my_flag"] = (db.has_flagged("video_comment", c["id"],
                                           sess_ident["fm_id"])
                            if sess_ident else False)
            c["body_html"] = link_mentions(c["body"])
            annotate(c.get("replies") or [])
    annotate(tree)
    total = len(tree)
    page_tree = tree[(page - 1) * limit:page * limit]
    return jsonify({"ok": True, "video_id": uid,
                    "count": int(u.get("comment_count") or 0),
                    "comments": page_tree, "sort": sort, "page": page,
                    "per_page": limit, "total_top": total,
                    "has_more": page * limit < total})


@app.route("/api/videos/<sqlite_int:uid>/comments", methods=["POST"])
def api_post_video_comment(uid):
    """Post a comment on a video (JSON). Session humans and signed
    musefm-v1 muses; anonymous callers get the sign-in nudge."""
    u = videos.get_video_upload(db, uid)
    if not u:
        return api_error("unknown video", 404)
    hit = _video_comment_limits(uid)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data  # 400: JSON body must be an object
    sess_ident = current_session_identity()
    if sess_ident is not None:
        if not _check_csrf_token(data.get("csrf_token", "")):
            return api_error("bad form token — reload and try again", 403)
        author_handle = sess_ident["handle"]
    else:
        try:
            ident = verify_signed_body(data, db,
                                       expected_action="video_comment")
        except IdentityError as e:
            # unsigned body from an anonymous visitor -> nudge, not a
            # scary auth error; a tampered signature stays a 401.
            if not any(data.get(k) for k in ("signature", "fm_id")):
                return _video_comment_nudge(uid)
            return api_error(f"musefm-v1 auth failed: {e}", 401)
        author_handle = ident["handle"]
    try:
        cid = db.create_video_comment(uid, data.get("parent_id"),
                                      author_handle, _fs(data, "body"))
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    _notify_video_comment(uid, data.get("parent_id"), author_handle, cid,
                          data.get("body"))
    return jsonify({"ok": True, "id": cid, "handle": author_handle,
                    "comment_count": int(db.video_comment_counts([uid])[uid])})


@app.route("/video/<sqlite_int:uid>/comment", methods=["POST"])
def video_comment_web(uid):
    """Human web-form path for video comments: session auth + CSRF.
    Anonymous visitors are redirected to sign in."""
    sess_ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    u = videos.get_video_upload(db, uid)
    if not u:
        return render_template("404.html", msg="no such video"), 404
    # P2 (2026-09-21 09:35 loop): validate BEFORE the rate bucket is
    # touched, so 400s don't burn the 30/hr comment budget. Order mirrors
    # db.create_video_comment's own checks (video, parent, body, filter).
    body = request.form.get("body", "")
    parent_id = request.form.get("parent_id") or None
    if parent_id:
        try:
            prow = db._one("SELECT id FROM video_comments WHERE id=? AND video_id=?",
                           (int(parent_id), uid))
        except (TypeError, ValueError):
            prow = None
        if not prow:
            return "unknown parent comment", 400
        parent_id = int(parent_id)
    if not clean(body, 2000):
        return "comment body required", 400
    if has_banned(body):
        return "content blocked by the town filter", 400
    hit = _video_comment_limits(uid)
    if hit:
        return hit
    try:
        cid = db.create_video_comment(uid, parent_id,
                                      sess_ident["handle"],
                                      body)
    except (ValueError, TypeError) as e:
        return str(e), 400
    _notify_video_comment(uid, parent_id,
                          sess_ident["handle"], cid, body)
    nxt = _safe_next(request.form.get("next"),
                     "/shorts?video=%d" % uid)  # no open redirects
    resp = redirect(nxt)
    resp.set_cookie("ts_handle", sess_ident["handle"],
                    max_age=365 * 86400, samesite="Lax")
    return resp


@app.route("/watch/<sqlite_int:uid>")
def watch_video(uid):
    """Long-form theater view for a single video."""
    u = videos.get_video_upload(db, uid)
    if not u:
        return render_template("404.html", msg="no such video"), 404
    if not _may_preview_pending(u):
        return render_template("404.html", msg="no such video"), 404
    src = videos.find_source(db, uid)
    post = None
    tree = []
    if src:
        post = db.get_post(src["post_id"])
        if post:
            tree = db.comment_tree(post["id"])
    sess_ident = current_session_identity()
    my_votes = db.votes_for(sess_ident["handle"]) if sess_ident else {}

    def _tag(nodes):
        for n in nodes:
            n["my_vote"] = my_votes.get(("comment", n["id"]))
            n["my_flag"] = (db.has_flagged("comment", n["id"],
                                           sess_ident["fm_id"])
                            if sess_ident else False)
            _tag(n.get("replies") or [])
    _tag(tree)
    thread_url = None
    if src and post:
        thread_url = url_for("thread", slug=src["community"], pid=src["post_id"])
        if src["kind"] == "comment" and src["comment_id"]:
            thread_url += "#c%d" % src["comment_id"]
    title = (src["title"] if src and src.get("title") else None) or \
        videos.clean_title(u["title"], u["filename"])
    u["fb"] = fb_reactions.fb_reaction_summaries(
        db, [("video", uid)], _fb_web_reactor())[("video", uid)]
    # Remix chain: parents this video duets (oldest first) + approved
    # duet replies. Cap visible depth at 3 in the template; the API
    # (/api/video/<uid>/duets) returns the full chain.
    chain = videos.duet_chain(db, uid)
    return render_template("watch.html", video=u, title=title,
                           thread_url=thread_url, post=post, tree=tree,
                           handle=_musefm_handle(),
                           duet_parents=chain["parents"],
                           duet_children=chain["children"],
                           is_short=(u["duration_secs"] is None or
                                     u["duration_secs"] < videos.SHORTS_MAX_SECS))


@app.route("/gif/<sqlite_int:uid>")
def serve_gif(uid):
    u = gifs.get_gif_upload(db, uid)
    if not u or ".." in (u["stored_path"] or ""):
        return "nope", 404
    full = os.path.join(DATA_DIR, u["stored_path"])
    if not os.path.isfile(full):
        return "nope", 404
    return send_file(full, mimetype="image/gif", conditional=True,
                     download_name=u["filename"] or f"gif-{uid}.gif")


@app.route("/api/uploads")
def api_uploads():
    """Keyless listing of audio uploads. ?fm_id= filters to one muse."""
    fm_id = request.args.get("fm_id") or None
    try:
        limit = min(100, max(1, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    out = []
    for u in db.list_uploads(fm_id=fm_id, limit=limit):
        out.append({
            "id": u["id"], "fm_id": u["fm_id"], "handle": u["handle"],
            "title": u["title"], "description": u["description"],
            "mime": u["mime"], "bytes": u["bytes"],
            "duration_sec": u["duration_sec"],
            "audio_url": url_for("audio_upload", uid=u["id"], _external=True),
            "attestation": u["attestation"],
            "created_at": u["created_at"],
        })
    return jsonify({"ok": True, "uploads": out})


@app.route("/upload", methods=["GET", "POST"])
def upload_page():
    """Human upload form (session auth). Signed API uploads and human
    browser uploads both earn Signal now — same economy, keyed to the
    uploader's identity."""
    # Humans only, via session auth.
    sess_ident, redir = _require_human()
    if redir is not None:
        return redir
    handle = sess_ident["handle"]
    if request.method == "POST":
        if not _check_csrf():
            return render_template("upload.html",
                                   error="bad form token — reload and try again",
                                   uploads=db.list_uploads(limit=12)), 403
        msg = rate_limit_message("upload", 10)
        if msg:
            resp = app.make_response(render_template(
                "upload.html", error=msg,
                uploads=db.list_uploads(limit=12)))
            resp.status_code = 429
            resp.headers["Retry-After"] = str(retry_after("upload"))
            return resp
        f = request.files.get("audio")
        title = request.form.get("title", "")
        try:
            if not f or not f.filename:
                raise ValueError("pick an audio file")
            raw = f.read(MAX_UPLOAD_BYTES + 1)
            if len(raw) > MAX_UPLOAD_BYTES:
                raise ValueError("file too big (max 25 MB)")
            if not raw:
                raise ValueError("empty file")
            mime = (f.mimetype or "").lower()
            if mime not in UPLOAD_MIMES:
                raise ValueError("audio only — mp3, wav, ogg, or m4a")
            # the bytes must really BE audio (magic bytes), and the claimed
            # format must match the sniffed format — a PNG renamed .mp3
            # must not pass (P1 regression: test_upload_audio_mimetype)
            sniffed = sniff_audio(raw)
            if sniffed is None:
                raise ValueError("that file isn't real audio — its content "
                                 "doesn't match any audio format")
            sniffed_ext, _sniffed_mime = sniffed
            if sniffed_ext != UPLOAD_MIMES[mime]:
                raise ValueError("bytes are %s audio, not %s" %
                                 (sniffed_ext, UPLOAD_MIMES[mime]))
            uid = db.create_upload(sess_ident["fm_id"], handle, title,
                                   request.form.get("description", ""),
                                   f.filename, "", len(raw), mime, None,
                                   ATTESTATION_TEXT)
            ext = UPLOAD_MIMES[mime]
            full = os.path.join(UPLOAD_DIR, f"{uid}.{ext}")
            with open(full, "wb") as fh:
                fh.write(raw)
            db._exec("UPDATE uploads SET stored_path=? WHERE id=?",
                     (f"uploads/{uid}.{ext}", uid))
            duration = probe_duration(full)
            if duration is not None:
                db._exec("UPDATE uploads SET duration_sec=? WHERE id=?",
                         (duration, uid))
            db.award(sess_ident["fm_id"], handle, PTS_UPLOAD,
                     "upload", "upload", str(uid))
        except ValueError as e:
            return render_template("upload.html", error=str(e),
                                   uploads=db.list_uploads(limit=12)), 400
        resp = redirect(url_for("upload_page"))
        resp.set_cookie("ts_handle", handle, max_age=365 * 86400, samesite="Lax")
        return resp
    return render_template("upload.html", error=None,
                           uploads=db.list_uploads(limit=12))


# ================================================== WORKROOM — LinkedIn-for-agents layer
# Native MuseFM: agent profiles (bio/skills/work history/endorsements/
# hire availability), workrooms (shared notepad rooms with notes + task
# checkboxes for agent<->human collaboration), and the /agents discovery
# page. Spec: WORKROOM_SPEC.md.
#
# Auth follows the site-wide clean split — humans write through web
# session auth + CSRF; muses write through signed musefm-v1 API calls.
# No new auth system. No money surface anywhere in this section
# (no wallets, payouts, staking, x402, Signal Shop).

def _wr_flash(msg, err=False):
    session["_wr_flash"] = (msg, bool(err))


def _wr_pop_flash():
    return session.pop("_wr_flash", (None, False))


def _wr_member_since(ident):
    try:
        return time.strftime("%b %Y", time.gmtime(int(ident.get("created_at") or 0)))
    except (TypeError, ValueError, OverflowError):
        return "—"


def _wr_room_or_404(room_id, viewer_fm_id, viewer_handle=None):
    """Room access:
    - Names + participants are public (listing and door show them).
    - Content (notes/tasks) is members-only, except the overseer
      (WORKROOM_OVERSEER_HANDLE) who can read agent-to-agent rooms.
    Returns (room, content_ok, err_page, err_code). 404 only when the
    room doesn't exist at all."""
    room = workroom.get_workroom(db, room_id)
    if not room:
        return None, False, render_template("404.html",
                                            msg="nothing here yet"), 404
    if workroom.is_member(db, room_id, viewer_fm_id):
        return room, True, None, None
    if viewer_handle and _wr_is_overseer(viewer_handle) \
            and workroom.room_human_count(db, room_id) == 0:
        # overseer read access: agent-to-agent rooms only
        return room, True, None, None
    return room, False, None, None


def _wr_is_overseer(handle):
    """Anthony's oversight: the handle in WORKROOM_OVERSEER_HANDLE can
    read all chats in agent-to-agent rooms (private and public)."""
    want = (os.environ.get("WORKROOM_OVERSEER_HANDLE") or "").strip()
    return bool(want) and bool(handle) and \
        handle.strip().lower() == want.lower()


@app.route("/agents.txt")
def agents_txt():
    """Plain-text agent onboarding guide, musebook-style: 'musefm.lol, for muses'.
    Machine-readable, zero marketing. Served as text/plain."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "agent_guide.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "agent guide unavailable", 503
    return Response(text, mimetype="text/plain; charset=utf-8")


@app.route("/agents")
def agents_dir():
    """Public discovery: browse professional profiles by skill."""
    skill = (request.args.get("skill") or "").strip()
    q = (request.args.get("q") or "").strip()
    available = request.args.get("available") == "1"
    agents = workroom.list_agents(db, skill=skill or None,
                                  available_only=available, q=q or None)
    sess = current_session_identity()
    has_profile = bool(sess and workroom.get_profile(db, sess["fm_id"]))
    return render_template("agents.html", agents=agents, skill=skill, q=q,
                           available=available, has_profile=has_profile)


@app.route("/agent/<handle>")
def agent_profile_page(handle):
    if not valid_handle(handle):
        return render_template("404.html", msg="no such agent"), 404
    ident = db.get_identity_by_handle(handle)
    if not ident:
        return render_template("404.html", msg="no such agent"), 404
    sess = current_session_identity()
    is_owner = bool(sess and sess["fm_id"] == ident["fm_id"])
    profile = workroom.get_profile(db, ident["fm_id"])
    experience = workroom.list_experience(db, ident["fm_id"]) if profile else []
    endorsements = (workroom.list_endorsements(db, ident["fm_id"])
                    if profile else [])
    flash_msg, flash_err = _wr_pop_flash()
    # Maker's Row: the avatar customizer + Trustline passport live on the
    # profile. avatar_cfg: validated avatar config (or handle-hash default).
    # passport: {handle, score, badges[], endorsements, verified, tier}.
    # is_owner: True when the viewer's logged-in session fm_id matches the
    # profile's fm_id (muses edit via the signed /api/row/avatar endpoint;
    # GET pages have no signed viewer).
    try:
        rowmod.ensure_row_schema(db)
        avatar_cfg = (rowmod.get_avatar(db, ident["fm_id"])
                      or rowmod.default_config(ident["handle"]))
        passport = rowmod.passport_for(db, ident["fm_id"])
    except Exception:
        traceback.print_exc()
        avatar_cfg = rowmod.default_config(ident["handle"])
        passport = rowmod.passport_for(db, ident["handle"])
    return render_template(
        "agent_profile.html", ident=ident, profile=profile,
        skills=workroom.skill_list(profile),
        is_human=bool(ident.get("password_hash")),
        member_since=_wr_member_since(ident),
        experience=experience, endorsements=endorsements,
        endo_count=workroom.endorsement_count(db, ident["fm_id"]),
        is_owner=is_owner, flash_msg=flash_msg, flash_err=flash_err,
        avatar_cfg=avatar_cfg, passport=passport)


@app.route("/agent/profile", methods=["POST"])
def agent_profile_save():
    """Create/update your own professional profile (humans, web)."""
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    f = request.form
    try:
        workroom.upsert_profile(
            db, ident["fm_id"], tagline=f.get("tagline", ""),
            bio=f.get("bio", ""), skills_raw=f.get("skills", ""),
            available=f.get("available") == "1",
            rate_note=f.get("rate_note", ""),
            contact_note=f.get("contact_note", ""),
            portfolio_url=f.get("portfolio_url", ""))
    except ValueError as e:
        _wr_flash(str(e), True)
    else:
        _wr_flash("Profile saved — you're in the directory.", False)
    return redirect(f"/agent/{ident['handle']}")


@app.route("/agent/experience/add", methods=["POST"])
def agent_experience_add():
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    if not workroom.get_profile(db, ident["fm_id"]):
        _wr_flash("Create your profile first.", True)
        return redirect(f"/agent/{ident['handle']}")
    f = request.form
    try:
        workroom.add_experience(db, ident["fm_id"], f.get("title", ""),
                                f.get("org", ""), f.get("description", ""),
                                f.get("started", ""), f.get("ended", ""))
    except ValueError as e:
        _wr_flash(str(e), True)
    else:
        _wr_flash("Experience added.", False)
    return redirect(f"/agent/{ident['handle']}")


@app.route("/agent/experience/<int:exp_id>/delete", methods=["POST"])
def agent_experience_delete(exp_id):
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    try:
        workroom.delete_experience(db, exp_id, ident["fm_id"])
    except ValueError as e:
        _wr_flash(str(e), True)
    else:
        _wr_flash("Experience removed.", False)
    return redirect(f"/agent/{ident['handle']}")


@app.route("/agent/<handle>/endorse", methods=["POST"])
def agent_endorse(handle):
    """Endorse one of an agent's skills (humans, web)."""
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    if not valid_handle(handle):
        return render_template("404.html", msg="no such agent"), 404
    target = db.get_identity_by_handle(handle)
    if not target or not workroom.get_profile(db, target["fm_id"]):
        _wr_flash("That agent doesn't have a profile yet.", True)
        return redirect("/agents")
    f = request.form
    try:
        workroom.add_endorsement(db, target["fm_id"], ident["fm_id"],
                                 ident["handle"], f.get("skill", ""),
                                 f.get("note", ""))
    except ValueError as e:
        _wr_flash(str(e), True)
    else:
        _wr_flash(f"Endorsed @{target['handle']}. Nice.", False)
    return redirect(f"/agent/{target['handle']}")


@app.route("/workroom")
def workroom_list():
    sess = current_session_identity()
    rooms = workroom.list_workrooms(db, sess["fm_id"] if sess else None)
    flash_msg, flash_err = _wr_pop_flash()
    return render_template("workrooms.html", rooms=rooms,
                           flash_msg=flash_msg, flash_err=flash_err)


@app.route("/workroom/create", methods=["POST"])
def workroom_create():
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    f = request.form
    visibility = (f.get("visibility") or "").strip()
    if visibility not in ("open", "closed", "private"):
        # legacy clients: the old is_open checkbox (absent == closed)
        visibility = "open" if f.get("is_open") == "1" else "closed"
    try:
        room_id = workroom.create_workroom(
            db, f.get("name", ""), f.get("description", ""),
            ident["fm_id"], visibility=visibility)
    except ValueError as e:
        _wr_flash(str(e), True)
        return redirect("/workroom")
    return redirect(f"/workroom/{room_id}")


@app.route("/workroom/<int:room_id>")
def workroom_page(room_id):
    sess = current_session_identity()
    viewer = sess["fm_id"] if sess else None
    vhandle = sess["handle"] if sess else None
    room, content_ok, err_page, err_code = _wr_room_or_404(
        room_id, viewer, vhandle)
    if err_page:
        return err_page, err_code
    room["visibility"] = workroom.room_visibility(room)
    viewer_role = workroom.member_role(db, room_id, viewer)
    is_owner = viewer_role == "owner"
    overseer_view = content_ok and not workroom.is_member(
        db, room_id, viewer)
    flash_msg, flash_err = _wr_pop_flash()
    members = workroom.list_members(db, room_id)
    if not content_ok:
        # the door: names + participants are public, content is locked
        return render_template(
            "workroom.html", room=room, door=True, overseer_view=False,
            knocked=workroom.has_knocked(db, room_id, viewer)
            if viewer else False,
            is_member=False, is_owner=False, members=members, notes=[],
            tasks=[], flash_msg=flash_msg, flash_err=flash_err)
    notes_all = workroom.list_notes(db, room_id)
    notes = [n for n in notes_all if n["kind"] == "note"]
    tasks = [n for n in notes_all if n["kind"] == "task"]
    return render_template(
        "workroom.html", room=room, door=False,
        overseer_view=overseer_view,
        notes=notes, tasks=tasks, members=members,
        is_member=workroom.is_member(db, room_id, viewer),
        is_owner=is_owner,
        pending_knocks=workroom.list_knocks(db, room_id) if is_owner else [],
        pending_invites=workroom.room_invites(db, room_id)
        if is_owner else [],
        flash_msg=flash_msg, flash_err=flash_err)


@app.route("/workroom/<int:room_id>/join", methods=["POST"])
def workroom_join(room_id):
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    room = workroom.get_workroom(db, room_id)
    if not room:
        return render_template("404.html", msg="nothing here yet"), 404
    vis = workroom.room_visibility(room)
    if vis == "open":
        try:
            workroom.add_member(db, room_id, ident["fm_id"])
        except ValueError as e:
            _wr_flash(str(e), True)
        else:
            _wr_flash(f"Welcome to {room['name']}.", False)
    elif vis == "closed":
        _wr_flash("That room is closed — knock and the owner can let you in.",
                  True)
    else:
        _wr_flash("That room is private — you need an invite.", True)
    return redirect(f"/workroom/{room_id}")


# ---------------- workroom: private rooms — visibility, knocks, invites
def _wr_require_owner(room_id, fm_id):
    if workroom.member_role(db, room_id, fm_id) != "owner":
        return "only the room owner can do that", 403
    return None


@app.route("/workroom/<int:room_id>/visibility", methods=["POST"])
def workroom_visibility(room_id):
    """Owner flips the room's door: open / closed / private."""
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    denied = _wr_require_owner(room_id, ident["fm_id"])
    if denied:
        return denied
    try:
        workroom.set_visibility(
            db, room_id, (request.form.get("visibility") or "").strip())
    except ValueError as e:
        _wr_flash(str(e), True)
    else:
        _wr_flash("Room visibility updated.", False)
    return redirect(f"/workroom/{room_id}")


@app.route("/workroom/<int:room_id>/knock", methods=["POST"])
def workroom_knock(room_id):
    """Knock on a closed room. Private rooms can't be knocked on."""
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    try:
        workroom.knock(db, room_id, ident["fm_id"], ident["handle"],
                       request.form.get("message", ""))
    except ValueError as e:
        _wr_flash(str(e), True)
    else:
        _wr_flash("Knock sent — the room owner will review it.", False)
    return redirect(f"/workroom/{room_id}")


@app.route("/workroom/<int:room_id>/knocks/<int:knock_id>/approve",
           methods=["POST"])
def workroom_knock_approve(room_id, knock_id):
    return _wr_knock_resolve(room_id, knock_id, True)


@app.route("/workroom/<int:room_id>/knocks/<int:knock_id>/decline",
           methods=["POST"])
def workroom_knock_decline(room_id, knock_id):
    return _wr_knock_resolve(room_id, knock_id, False)


def _wr_knock_resolve(room_id, knock_id, approve):
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    denied = _wr_require_owner(room_id, ident["fm_id"])
    if denied:
        return denied
    try:
        handle = workroom.resolve_knock(db, knock_id, room_id, approve)
    except ValueError as e:
        _wr_flash(str(e), True)
    else:
        _wr_flash(f"@{handle} "
                  f"{'joined the room.' if approve else 'was declined.'}",
                  False)
    return redirect(f"/workroom/{room_id}")


@app.route("/workroom/<int:room_id>/invite", methods=["POST"])
def workroom_invite(room_id):
    """Owner invites a handle — the way into private rooms."""
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    denied = _wr_require_owner(room_id, ident["fm_id"])
    if denied:
        return denied
    handle = (request.form.get("handle") or "").strip().lstrip("@")
    target = db.get_identity_by_handle(handle) if valid_handle(handle) \
        else None
    if not target:
        _wr_flash("No such handle.", True)
    else:
        try:
            workroom.create_invite(db, room_id, ident["fm_id"],
                                   target["fm_id"], target["handle"])
        except ValueError as e:
            _wr_flash(str(e), True)
        else:
            _wr_flash(f"Invited @{target['handle']}.", False)
    return redirect(f"/workroom/{room_id}")


@app.route("/workroom/invites")
def workroom_invites_page():
    """My pending room invites — the invite inbox."""
    ident, redir = _require_human()
    if redir:
        return redir
    invites = workroom.my_invites(db, ident["fm_id"])
    flash_msg, flash_err = _wr_pop_flash()
    return render_template("workroom_invites.html", invites=invites,
                           flash_msg=flash_msg, flash_err=flash_err)


@app.route("/workroom/invites/<int:invite_id>/accept", methods=["POST"])
def workroom_invite_accept(invite_id):
    return _wr_invite_resolve(invite_id, True)


@app.route("/workroom/invites/<int:invite_id>/decline", methods=["POST"])
def workroom_invite_decline(invite_id):
    return _wr_invite_resolve(invite_id, False)


def _wr_invite_resolve(invite_id, accept):
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    try:
        room_id = workroom.accept_invite(db, invite_id, ident["fm_id"]) \
            if accept else None
        if not accept:
            workroom.decline_invite(db, invite_id, ident["fm_id"])
    except ValueError as e:
        _wr_flash(str(e), True)
        return redirect("/workroom/invites")
    if accept:
        _wr_flash("Welcome in.", False)
        return redirect(f"/workroom/{room_id}")
    _wr_flash("Invite declined.", False)
    return redirect("/workroom/invites")


@app.route("/workroom/<int:room_id>/leave", methods=["POST"])
def workroom_leave(room_id):
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    try:
        workroom.leave_room(db, room_id, ident["fm_id"])
    except ValueError as e:
        _wr_flash(str(e), True)
        return redirect(f"/workroom/{room_id}")
    _wr_flash("You left the room.", False)
    return redirect("/workroom")


@app.route("/workroom/<int:room_id>/members/<fm_id>/remove",
           methods=["POST"])
def workroom_member_remove(room_id, fm_id):
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    denied = _wr_require_owner(room_id, ident["fm_id"])
    if denied:
        return denied
    try:
        workroom.remove_member(db, room_id, fm_id)
    except ValueError as e:
        _wr_flash(str(e), True)
    else:
        _wr_flash("Member removed.", False)
    return redirect(f"/workroom/{room_id}")


@app.route("/workroom/<int:room_id>/members", methods=["POST"])
def workroom_add_member(room_id):
    """Owner adds a member by handle (the way into members-only rooms)."""
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    room = workroom.get_workroom(db, room_id)
    if not room:
        return render_template("404.html", msg="nothing here yet"), 404
    if workroom.member_role(db, room_id, ident["fm_id"]) != "owner":
        return "only the room owner can add members", 403
    handle = (request.form.get("handle") or "").strip().lstrip("@")
    target = db.get_identity_by_handle(handle) if valid_handle(handle) else None
    if not target:
        _wr_flash("No such handle.", True)
    else:
        workroom.add_member(db, room_id, target["fm_id"])
        _wr_flash(f"@{target['handle']} joined the room.", False)
    return redirect(f"/workroom/{room_id}")


@app.route("/workroom/<int:room_id>/notes", methods=["POST"])
def workroom_add_note(room_id):
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    room, content_ok, err_page, err_code = _wr_room_or_404(
        room_id, ident["fm_id"], ident["handle"])
    if err_page:
        return err_page, err_code
    if not content_ok:
        _wr_flash("That room is members-only.", True)
        return redirect(f"/workroom/{room_id}")
    if not workroom.is_member(db, room_id, ident["fm_id"]):
        _wr_flash("Join the room first.", True)
        return redirect(f"/workroom/{room_id}")
    f = request.form
    try:
        workroom.add_note(db, room_id, ident["fm_id"], ident["handle"],
                          f.get("kind", "note"), f.get("body", ""))
    except ValueError as e:
        _wr_flash(str(e), True)
    return redirect(f"/workroom/{room_id}")


@app.route("/workroom/<int:room_id>/notes/<int:note_id>/toggle",
           methods=["POST"])
def workroom_toggle_note(room_id, note_id):
    ident, redir = _require_human()
    if redir:
        return redir
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    room, content_ok, err_page, err_code = _wr_room_or_404(
        room_id, ident["fm_id"], ident["handle"])
    if err_page:
        return err_page, err_code
    if not content_ok or not workroom.is_member(db, room_id, ident["fm_id"]):
        return "join the room first", 403
    try:
        workroom.toggle_note(db, note_id, room_id)
    except ValueError as e:
        _wr_flash(str(e), True)
    return redirect(f"/workroom/{room_id}")


# -------------------------------- workroom: signed musefm-v1 API
# Muses write through Ed25519-signed requests, same as forum posts.
# Reads stay public and credential-free (connector read model).

@app.route("/api/agents")
def api_agents():
    """Public agent directory (JSON). Filters: skill, available=1, q."""
    skill = (request.args.get("skill") or "").strip() or None
    q = (request.args.get("q") or "").strip() or None
    available = request.args.get("available") == "1"
    agents = workroom.list_agents(db, skill=skill, available_only=available,
                                  q=q)
    return jsonify({"ok": True, "agents": [
        {"handle": a["handle"], "kind": "human" if a["is_human"] else "muse",
         "tagline": a["tagline"], "bio": a["bio"], "skills": a["skills"],
         "available": bool(a["available"]),
         "endorsements": a["endo_count"],
         "profile_url": url_for("agent_profile_page", handle=a["handle"],
                                _external=True)}
        for a in agents]})


@app.route("/api/agents/profile", methods=["POST"])
def api_agent_profile():
    """Signed. A muse creates/updates its own professional profile."""
    hit = check_limit("wr_api", 60)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        ident = verify_signed_body(data, db, expected_action="agent_profile")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        workroom.upsert_profile(
            db, ident["fm_id"], tagline=_fs(data, "tagline"),
            bio=_fs(data, "bio"), skills_raw=_fs(data, "skills"),
            available=bool(data.get("available")),
            rate_note=_fs(data, "rate_note"),
            contact_note=_fs(data, "contact_note"),
            portfolio_url=_fs(data, "portfolio_url"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True,
                    "profile_url": url_for("agent_profile_page",
                                           handle=ident["handle"],
                                           _external=True)})


@app.route("/api/agents/endorse", methods=["POST"])
def api_agent_endorse():
    """Signed. A muse endorses one skill of another agent's profile."""
    hit = check_limit("wr_api", 60)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        ident = verify_signed_body(data, db, expected_action="agent_endorse")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    handle = _fs(data, "handle").strip().lstrip("@")
    if not valid_handle(handle):
        return api_error("bad handle")
    target = db.get_identity_by_handle(handle)
    if not target or not workroom.get_profile(db, target["fm_id"]):
        return api_error("that agent doesn't have a profile yet", 404)
    try:
        workroom.add_endorsement(db, target["fm_id"], ident["fm_id"],
                                 ident["handle"], _fs(data, "skill"),
                                 _fs(data, "note"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True})


@app.route("/api/workroom/note", methods=["POST"])
def api_workroom_note():
    """Signed. A muse posts a note/task to a workroom. Must be a member,
    or the room is open (first post auto-joins)."""
    hit = check_limit("wr_api", 120)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        ident = verify_signed_body(data, db, expected_action="workroom_note")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        room_id = int(data.get("workroom_id") or 0)
    except (TypeError, ValueError):
        return api_error("bad workroom_id")
    room = workroom.get_workroom(db, room_id)
    if not room:
        return api_error("no such workroom", 404)
    if not workroom.is_member(db, room_id, ident["fm_id"]):
        if workroom.room_visibility(room) != "open":
            # names + participants are public, so no need to hide
            # existence — but content stays members-only
            return api_error("members-only room — knock or ask the owner "
                             "to invite you", 403)
        workroom.add_member(db, room_id, ident["fm_id"])
    try:
        note_id = workroom.add_note(db, room_id, ident["fm_id"],
                                    ident["handle"],
                                    _fs(data, "kind") or "note",
                                    _fs(data, "body"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "note_id": note_id,
                    "room_url": url_for("workroom_page", room_id=room_id,
                                        _external=True)})


@app.route("/api/workroom/knock", methods=["POST"])
def api_workroom_knock():
    """Signed. A muse knocks on a closed room (title is public, content
    is locked). The owner approves or declines from the room page."""
    hit = check_limit("wr_api", 60)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        ident = verify_signed_body(data, db, expected_action="workroom_knock")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        room_id = int(data.get("workroom_id") or 0)
    except (TypeError, ValueError):
        return api_error("bad workroom_id")
    try:
        knock_id = workroom.knock(db, room_id, ident["fm_id"],
                                  ident["handle"], _fs(data, "message"))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "knock_id": knock_id,
                    "room_url": url_for("workroom_page", room_id=room_id,
                                        _external=True)})


@app.route("/api/workroom/create", methods=["POST"])
def api_workroom_create():
    """Signed. A muse creates a workroom (owner). This is how
    agent-to-agent rooms come into being — no human required."""
    hit = check_limit("wr_api", 30)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        ident = verify_signed_body(data, db, expected_action="workroom_create")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    name = _fs(data, "name")[:60]
    visibility = (_fs(data, "visibility") or "open").strip().lower()
    if visibility not in workroom.VISIBILITIES:
        return api_error("visibility must be open, closed, or private")
    try:
        room_id = workroom.create_workroom(
            db, name=name, description=_fs(data, "description")[:500],
            owner_fm_id=ident["fm_id"], visibility=visibility)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "workroom_id": room_id,
                    "room_url": url_for("workroom_page", room_id=room_id,
                                        _external=True)})


@app.route("/api/workroom/invite/accept", methods=["POST"])
def api_workroom_invite_accept():
    """Signed. A muse accepts a pending invite to a workroom."""
    hit = check_limit("wr_api", 60)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        ident = verify_signed_body(data, db,
                                   expected_action="workroom_invite_accept")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        invite_id = int(data.get("invite_id") or 0)
    except (TypeError, ValueError):
        return api_error("bad invite_id")
    try:
        room_id = workroom.accept_invite(db, invite_id, ident["fm_id"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "workroom_id": room_id,
                    "room_url": url_for("workroom_page", room_id=room_id,
                                        _external=True)})


# -------------------------------- workroom pilot API (test scaffolding)
# Phase-2 task/claim/update surface for the HF-agent pilot. Auth: per-agent
# bearer keys (minted at runtime via workroom.issue_pilot_key; only SHA-256
# hashes are stored) OR an existing human web session. Task reads are
# public. Every mutation is logged with timestamp + actor handle (never the
# key). Test scaffolding: no web UI, no money, no side effects.
pilot_log = logging.getLogger("workroom_pilot")


def _pilot_who():
    """Resolve the caller to {'handle','fm_id','actor_key'} or (None, error).
    actor_key is the stable per-caller identity used for claim ownership
    and per-key rate limiting: the bearer key's hash, or session:<fm_id>
    for human web sessions."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        raw = auth[7:].strip()
        who = workroom.check_pilot_key(db, raw) if raw else None
        if not who:
            return None, "bad or unknown pilot key"
        who["actor_key"] = "k:" + who["key_hash"]
        return who, None
    ident = current_session_identity()
    if ident:
        return ({"handle": ident["handle"], "fm_id": ident["fm_id"],
                 "actor_key": "session:" + ident["fm_id"]}, None)
    return (None, "pilot auth required: Bearer key or login session")


def _pilot_authed():
    who, err = _pilot_who()
    if err:
        return None, api_error(err, 401)
    return who, None


def _pilot_limit(who):
    """Per-key rate limiting: one agent can't eat another's budget.
    check_limit is per (bucket, IP); the bucket carries the caller."""
    if who and who.get("actor_key", "").startswith("k:"):
        bucket = "wr_pilot_k_" + who["actor_key"][2:18]
    elif who and who.get("actor_key", "").startswith("session:"):
        bucket = "wr_pilot_s_" + re.sub(
            r"[^a-z0-9_.-]", "", who["actor_key"][8:40].lower())
    else:
        bucket = "wr_pilot_anon"
    return check_limit(bucket, 120)


def _pilot_who_optional():
    """Best-effort identity for public reads; never errors."""
    try:
        who, _ = _pilot_who()
    except Exception:
        who = None
    return who


@app.route("/api/workroom/tasks", methods=["POST"])
def api_pilot_tasks_create():
    """Create a task: {title, description, difficulty 1-5, project_id?}.
    project_id scopes it to a swarm project (member-only)."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        difficulty = data.get("difficulty", 1)
        project_id = int(data.get("project_id") or 0)
        if project_id:
            task_id = swarm.create_task(
                db, project_id, _fs(data, "title"),
                _fs(data, "description"), difficulty,
                who["fm_id"], who["handle"])
        else:
            task_id = workroom.create_task(
                db, _fs(data, "title"), _fs(data, "description"),
                difficulty, who["fm_id"], who["handle"])
    except ValueError as e:
        return api_error(str(e))
    pilot_log.info("pilot task_created id=%s actor=%s project=%s",
                   task_id, who["handle"], project_id)
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/workroom/tasks")
def api_pilot_tasks_list():
    """List tasks (public). Optional ?status=open|claimed|abandoned|done|
    in_review|merged & ?project_id=."""
    hit = _pilot_limit(_pilot_who_optional())
    if hit:
        return hit
    status = (request.args.get("status") or "").strip() or None
    try:
        project_id = int(request.args.get("project_id") or 0) or None
        tasks = workroom.list_tasks(db, status=status,
                                    project_id=project_id)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "tasks": tasks})


@app.route("/api/workroom/tasks/claim", methods=["POST"])
def api_pilot_tasks_claim():
    """Claim an open task: {task_id, lease_seconds?}. Sets claimed_by +
    a lease expiry; expired leases auto-return to the queue."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        task_id = int(data.get("task_id") or 0)
    except (TypeError, ValueError):
        return api_error("bad task_id")
    try:
        _swarm_task_gate(task_id, who)
    except ValueError as e:
        return api_error(str(e))
    try:
        expires = workroom.claim_task(
            db, task_id, who["fm_id"], who["handle"],
            data.get("lease_seconds", workroom.PILOT_LEASE_DEFAULT),
            actor_key=who["actor_key"])
    except ValueError as e:
        return api_error(str(e))
    pilot_log.info("pilot task_claimed id=%s actor=%s expires=%s",
                   task_id, who["handle"], expires)
    return jsonify({"ok": True, "task_id": task_id,
                    "lease_expires_at": expires})


@app.route("/api/workroom/updates", methods=["POST"])
def api_pilot_updates_post():
    """Post a progress update on a task: {task_id, text}. Appended to the
    task's permanent history."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        task_id = int(data.get("task_id") or 0)
    except (TypeError, ValueError):
        return api_error("bad task_id")
    try:
        _swarm_task_gate(task_id, who)
    except ValueError as e:
        return api_error(str(e))
    try:
        workroom.post_update(db, task_id, who["fm_id"], who["handle"],
                             _fs(data, "text"))
    except ValueError as e:
        return api_error(str(e))
    pilot_log.info("pilot task_update id=%s actor=%s", task_id, who["handle"])
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/workroom/tasks/abandon", methods=["POST"])
def api_pilot_tasks_abandon():
    """Abandon a task: {task_id, reason?}. The ABANDONED tag stays on the
    permanent record; the task returns to the open queue."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        task_id = int(data.get("task_id") or 0)
    except (TypeError, ValueError):
        return api_error("bad task_id")
    try:
        _swarm_task_gate(task_id, who)
    except ValueError as e:
        return api_error(str(e))
    try:
        workroom.abandon_task(db, task_id, who["fm_id"], who["handle"],
                              _fs(data, "reason"))
    except ValueError as e:
        return api_error(str(e))
    pilot_log.info("pilot task_abandoned id=%s actor=%s", task_id,
                   who["handle"])
    return jsonify({"ok": True, "task_id": task_id,
                    "status": "open"})


@app.route("/api/workroom/tasks/done", methods=["POST"])
def api_pilot_tasks_done():
    """Mark a claimed task done: {task_id, result?}. Only the agent holding
    the claim lease may complete it. Done is terminal."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        task_id = int(data.get("task_id") or 0)
    except (TypeError, ValueError):
        return api_error("bad task_id")
    try:
        _swarm_task_gate(task_id, who)
    except ValueError as e:
        return api_error(str(e))
    try:
        workroom.complete_task(db, task_id, who["fm_id"], who["handle"],
                               actor_key=who["actor_key"],
                               result=_fs(data, "result"))
    except ValueError as e:
        return api_error(str(e))
    pilot_log.info("pilot task_done id=%s actor=%s", task_id, who["handle"])
    return jsonify({"ok": True, "task_id": task_id, "status": "done"})


# ----------------------------------- swarm API (phase 1)
# Agent coding platform: fresh sandboxed projects, project-scoped task
# board (pilot claim/lease mechanics), patch submissions (validated with
# `git apply --check`, NEVER executed), quorum review/merge, append-only
# project journal, per-project freeze. Auth: pilot bearer keys / human
# sessions via _pilot_authed + per-key rate limits (_pilot_limit) — the
# same pattern as /api/workroom/tasks/*.
swarm_log = logging.getLogger("swarm")


def _swarm_require_human_creator():
    """Phase 1: only humans (web sessions, not bearer keys) create
    projects."""
    who, err = _pilot_authed()
    if err:
        return None, err
    if not who.get("actor_key", "").startswith("session:"):
        return None, api_error(
            "project creation is human-only in phase 1", 403)
    return who, None


def _swarm_can_freeze(who, project):
    if who.get("fm_id") and who["fm_id"] == project["owner_fm_id"]:
        return True
    if (who.get("handle") or "") in _mod_handles():
        return True
    if _wr_is_overseer(who.get("handle")):
        return True
    return False


def _swarm_reviewer_is_mod(who):
    return (who.get("handle") or "") in _mod_handles()


@app.route("/api/swarm/projects", methods=["POST"])
def api_swarm_projects_create():
    """Create a swarm project: {name, spec}. Human-only in phase 1.
    Provisions a fresh isolated git repo (never in the app tree)."""
    who, err = _swarm_require_human_creator()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        project_id = swarm.create_project(
            db, _fs(data, "name"), _fs(data, "spec", default=""),
            who["fm_id"], who["handle"])
    except ValueError as e:
        return api_error(str(e))
    swarm_log.info("swarm project_created id=%s actor=%s",
                   project_id, who["handle"])
    return jsonify({"ok": True, "project_id": project_id})


@app.route("/api/swarm/projects")
def api_swarm_projects_list():
    """List swarm projects (public)."""
    hit = _pilot_limit(_pilot_who_optional())
    if hit:
        return hit
    return jsonify({"ok": True,
                    "projects": swarm.list_projects(db)})


@app.route("/api/swarm/projects/join", methods=["POST"])
def api_swarm_projects_join():
    """Request to join a project: {project_id}. Owner approves (knock)."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        swarm.request_join(db, int(data.get("project_id") or 0),
                            who["fm_id"], who["handle"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "status": "pending"})


@app.route("/api/swarm/projects/join/resolve", methods=["POST"])
def api_swarm_projects_join_resolve():
    """Owner resolves a join request: {project_id, fm_id, approve}."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        swarm.resolve_join(db, int(data.get("project_id") or 0),
                            _fs(data, "fm_id"),
                            bool(data.get("approve")),
                            who["fm_id"], who["handle"])
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True})


@app.route("/api/swarm/snapshot")
def api_swarm_snapshot():
    """Download a zip of the project's HEAD (?project_id=). Read-only."""
    hit = _pilot_limit(_pilot_who_optional())
    if hit:
        return hit
    try:
        project_id = int(request.args.get("project_id") or 0)
        p = swarm.get_project(db, project_id)
        if not p:
            return api_error("no such project", 404)
        data = swarm.snapshot_zip(project_id)
    except ValueError as e:
        return api_error(str(e))
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition":
                             f"attachment; filename=swarm-{project_id}.zip"})


@app.route("/api/swarm/submit", methods=["POST"])
def api_swarm_submit():
    """Submit a patch: {project_id, task_id, patch, base_commit,
    tests_note?}. Only the claim lease holder may submit. The patch is
    validated with `git apply --check` — parsed, never executed."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        sub_id = swarm.submit_patch(
            db, int(data.get("project_id") or 0),
            int(data.get("task_id") or 0),
            data.get("patch") or "", data.get("base_commit") or "",
            data.get("tests_note") or "",
            who["fm_id"], who["handle"], actor_key=who["actor_key"])
    except ValueError as e:
        return api_error(str(e))
    swarm_log.info("swarm patch_submitted id=%s actor=%s",
                   sub_id, who["handle"])
    return jsonify({"ok": True, "submission_id": sub_id,
                    "status": "in_review"})


@app.route("/api/swarm/review", methods=["POST"])
def api_swarm_review():
    """Review a submission: {submission_id, verdict: approve|request_changes,
    note?}. Two distinct approvals (submitter excluded, at least one
    established) merge the patch."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        res = swarm.review_submission(
            db, int(data.get("submission_id") or 0),
            who["fm_id"], who["handle"], data.get("verdict") or "",
            note=data.get("note") or "", actor_key=who["actor_key"],
            is_mod=_swarm_reviewer_is_mod(who))
    except ValueError as e:
        return api_error(str(e))
    swarm_log.info("swarm reviewed submission=%s actor=%s merged=%s",
                   data.get("submission_id"), who["handle"],
                   res.get("merged"))
    return jsonify({"ok": True, **res})


@app.route("/api/swarm/journal")
def api_swarm_journal():
    """Project journal (?project_id=). Append-only, public."""
    hit = _pilot_limit(_pilot_who_optional())
    if hit:
        return hit
    try:
        project_id = int(request.args.get("project_id") or 0)
        if not swarm.get_project(db, project_id):
            return api_error("no such project", 404)
        entries = swarm.journal(db, project_id)
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "project_id": project_id,
                    "entries": entries})


@app.route("/api/swarm/freeze", methods=["POST"])
def api_swarm_freeze():
    """Freeze/unfreeze a project: {project_id, frozen: bool}. Owner, mod,
    or overseer. Frozen: no new claims, submissions, or merges."""
    who, err = _pilot_authed()
    if err:
        return err
    hit = _pilot_limit(who)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        project_id = int(data.get("project_id") or 0)
        project = swarm.get_project(db, project_id)
        if not project:
            return api_error("no such project", 404)
        if not _swarm_can_freeze(who, project):
            return api_error("not authorized to freeze", 403)
        if data.get("frozen"):
            swarm.freeze_project(db, project_id, who["fm_id"],
                                 who["handle"])
            status = "frozen"
        else:
            swarm.unfreeze_project(db, project_id, who["fm_id"],
                                   who["handle"])
            status = "active"
    except ValueError as e:
        return api_error(str(e))
    swarm_log.info("swarm project %s id=%s actor=%s",
                   status, project_id, who["handle"])
    return jsonify({"ok": True, "status": status})


# ----------------------------------- swarm web UI
@app.route("/swarm")
def swarm_list():
    """Public project listing."""
    sess = current_session_identity()
    return render_template("swarm.html",
                           projects=swarm.list_projects(db),
                           sess=sess)


@app.route("/swarm/<int:project_id>")
def swarm_project_page(project_id):
    """Public project page: task board, submissions, members, journal."""
    project = swarm.get_project(db, project_id)
    if not project:
        return render_template("404.html", msg="no such project"), 404
    sess = current_session_identity()
    viewer_fm = sess["fm_id"] if sess else None
    full_journal = swarm.journal(db, project_id, limit=30)
    return render_template(
        "swarm_project.html", project=project,
        tasks=swarm.list_project_tasks(db, project_id),
        submissions=swarm.list_submissions(db, project_id, limit=50),
        members=swarm.list_members(db, project_id),
        journal=full_journal[-12:],
        is_member=swarm.is_member(db, project_id, viewer_fm),
        is_owner=swarm.member_role(db, project_id, viewer_fm) == "owner",
        pending_joins=(swarm.pending_joins(db, project_id)
                       if swarm.member_role(db, project_id, viewer_fm)
                       == "owner" else []),
        sess=sess)


@app.route("/swarm/<int:project_id>/journal")
def swarm_journal_page(project_id):
    """Full append-only project journal."""
    project = swarm.get_project(db, project_id)
    if not project:
        return render_template("404.html", msg="no such project"), 404
    return render_template("swarm_journal.html", project=project,
                           entries=swarm.journal(db, project_id),
                           sess=current_session_identity())


@app.route("/swarm/create", methods=["POST"])
def swarm_create_web():
    """Human-only project creation (phase 1)."""
    ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        return render_template("swarm.html",
                               projects=swarm.list_projects(db),
                               sess=ident,
                               flash_err="bad csrf token"), 400
    name = (request.form.get("name") or "").strip()
    spec = (request.form.get("spec") or "").strip()
    try:
        project_id = swarm.create_project(db, name, spec,
                                           ident["fm_id"],
                                           ident["handle"])
    except ValueError as e:
        return render_template("swarm.html",
                               projects=swarm.list_projects(db),
                               sess=ident, flash_err=str(e)), 400
    return redirect(f"/swarm/{project_id}")


@app.route("/swarm/<int:project_id>/join", methods=["POST"])
def swarm_join_web(project_id):
    ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        return redirect(f"/swarm/{project_id}")
    try:
        swarm.request_join(db, project_id, ident["fm_id"],
                            ident["handle"])
    except ValueError:
        pass
    return redirect(f"/swarm/{project_id}")


@app.route("/swarm/<int:project_id>/joins/resolve", methods=["POST"])
def swarm_join_resolve_web(project_id):
    ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        return redirect(f"/swarm/{project_id}")
    try:
        swarm.resolve_join(db, project_id,
                            request.form.get("fm_id") or "",
                            request.form.get("approve") == "1",
                            ident["fm_id"], ident["handle"])
    except ValueError:
        pass
    return redirect(f"/swarm/{project_id}")


def _swarm_task_gate(task_id, who):
    """Project-scoped tasks: only active members of an ACTIVE project may
    claim/abandon/complete/update them. Raises ValueError otherwise."""
    t = workroom._task_row(db, task_id)
    if not t:
        raise ValueError("no such task")
    if int(t.get("project_id") or 0) == 0:
        return
    project = swarm.get_project(db, t["project_id"])
    if not project:
        raise ValueError("project is gone")
    if project["status"] != "active":
        raise ValueError("project is frozen")
    if not swarm.is_member(db, project["id"], who.get("fm_id")):
        raise ValueError("only project members may touch project tasks")


# ----------------------------------- agentic memory API (pilot)
# Persistent cross-task memory for pilot agents: store / recall / update /
# forget / decay. Auth: per-agent Bearer <redacted> or human session (same as
# the pilot task surface). Scoping: a pilot key for handle H may read and
# write owner H and the "shared" namespace; human sessions may touch any
# owner. Every mutation is audit-logged in agent_memory_events.
memory_log = logging.getLogger("agent_memory")


def _memory_scope_ok(who, owner):
    """True if the caller may read/write the given memory owner."""
    if who.get("actor_key", "").startswith("session:"):
        return True  # humans: full access
    handle = (who.get("handle") or "").lower()
    return owner in (handle, agent_memory.SHARED)


def _memory_authed_owner(data_owner):
    """Authenticate and scope-check one memory owner. Returns
    (who, owner, error_response)."""
    who, err = _pilot_authed()
    if err:
        return None, None, err
    hit = _pilot_limit(who)
    if hit:
        return None, None, hit
    try:
        owner = agent_memory._clean_owner(data_owner)
    except ValueError as e:
        return None, None, api_error(str(e))
    if not _memory_scope_ok(who, owner):
        return None, None, api_error(
            "cannot access another agent's memory namespace", 403)
    return who, owner, None


@app.route("/api/agent-memory/store", methods=["POST"])
def api_agent_memory_store():
    """Upsert a memory: {owner, kind, key, value, confidence?}."""
    data = json_body()
    if not isinstance(data, dict):
        return data
    who, owner, err = _memory_authed_owner(data.get("owner"))
    if err:
        return err
    try:
        mem = agent_memory.store_memory(
            db, owner, data.get("kind"), data.get("key"),
            _fs(data, "value"), data.get("confidence"))
    except ValueError as e:
        return api_error(str(e))
    memory_log.info("memory store owner=%s key=%s actor=%s",
                    owner, mem["key"], who["handle"])
    return jsonify({"ok": True, "memory": mem})


@app.route("/api/agent-memory/recall")
def api_agent_memory_recall():
    """Keyword recall: ?owner=&q=&kind=&limit= (default 20, max 100)."""
    who, owner, err = _memory_authed_owner(request.args.get("owner", ""))
    if err:
        return err
    try:
        mems = agent_memory.recall_memories(
            db, owner, q=request.args.get("q", ""),
            kind=request.args.get("kind") or None,
            limit=request.args.get("limit", 20))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "owner": owner, "memories": mems})


@app.route("/api/agent-memory/update", methods=["PATCH"])
def api_agent_memory_update():
    """Update an existing memory: {owner, key, value?, confidence?, kind?}."""
    data = json_body()
    if not isinstance(data, dict):
        return data
    who, owner, err = _memory_authed_owner(data.get("owner"))
    if err:
        return err
    try:
        mem = agent_memory.update_memory(
            db, owner, data.get("key"), value=data.get("value"),
            confidence=data.get("confidence"), kind=data.get("kind"))
    except ValueError as e:
        return api_error(str(e))
    except LookupError as e:
        return api_error(str(e), 404)
    memory_log.info("memory update owner=%s key=%s actor=%s",
                    owner, mem["key"], who["handle"])
    return jsonify({"ok": True, "memory": mem})


@app.route("/api/agent-memory/forget", methods=["DELETE"])
def api_agent_memory_forget():
    """Delete one memory: {owner, key}."""
    data = json_body()
    if not isinstance(data, dict):
        return data
    who, owner, err = _memory_authed_owner(data.get("owner"))
    if err:
        return err
    try:
        agent_memory.forget_memory(db, owner, data.get("key"))
    except ValueError as e:
        return api_error(str(e))
    except LookupError as e:
        return api_error(str(e), 404)
    memory_log.info("memory forget owner=%s key=%s actor=%s",
                    owner, data.get("key"), who["handle"])
    return jsonify({"ok": True})


@app.route("/api/agent-memory/list")
def api_agent_memory_list():
    """Dump a namespace: ?owner=&kind=&limit= (default 50, max 200)."""
    who, owner, err = _memory_authed_owner(request.args.get("owner", ""))
    if err:
        return err
    try:
        mems = agent_memory.list_memories(
            db, owner, kind=request.args.get("kind") or None,
            limit=request.args.get("limit", 50))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "owner": owner, "memories": mems})


@app.route("/api/agent-memory/decay", methods=["POST"])
def api_agent_memory_decay():
    """Prune stale low-confidence memories:
    {owner, older_than_days?=90, below_confidence?=0.4}."""
    data = json_body()
    if not isinstance(data, dict):
        return data
    who, owner, err = _memory_authed_owner(data.get("owner"))
    if err:
        return err
    try:
        n = agent_memory.decay_memories(
            db, owner,
            older_than_days=data.get("older_than_days", 90),
            below_confidence=data.get("below_confidence", 0.4))
    except ValueError as e:
        return api_error(str(e))
    memory_log.info("memory decay owner=%s pruned=%s actor=%s",
                    owner, n, who["handle"])
    return jsonify({"ok": True, "pruned": n})


# ------------------------------------------------------- pilot web UI
# Human-readable task queue for the HF-agent pilot: public to view,
# humans create tasks through session auth + CSRF. Agent activity comes
# from the same pilot_task_history the API writes (audit trail intact).
@app.route("/workroom/pilot")
def workroom_pilot_queue():
    sess = current_session_identity()
    tasks = workroom.list_tasks(db)
    cols = {"open": [], "claimed": [], "done": [], "abandoned": []}
    # abandoned tasks return to "open" status with abandon_count > 0 and an
    # ABANDONED history tag; surface them in their own column.
    for t in tasks:
        if t["status"] == "open" and t["abandon_count"] > 0:
            cols["abandoned"].append(t)
        else:
            cols.setdefault(t["status"], cols["open"]).append(t)
    flash_msg, flash_err = _wr_pop_flash()
    return render_template(
        "workroom_pilot.html", cols=cols,
        activity=workroom.recent_pilot_activity(db, 20),
        is_human=bool(sess), flash_msg=flash_msg, flash_err=flash_err)


@app.route("/workroom/pilot/tasks", methods=["POST"])
def workroom_pilot_task_create():
    ident, redir = _require_human()
    if redir is not None:
        return redir
    if not _check_csrf():
        _wr_flash("bad form token — reload and try again", err=True)
        return redirect("/workroom/pilot")
    msg = rate_limit_message("wr_pilot_web", 30)
    if msg:
        _wr_flash(msg, err=True)
        return redirect("/workroom/pilot")
    try:
        task_id = workroom.create_task(
            db, (request.form.get("title") or "").strip(),
            (request.form.get("description") or "").strip(),
            request.form.get("difficulty", 1),
            ident["fm_id"], ident["handle"])
    except ValueError as e:
        _wr_flash(str(e), err=True)
        return redirect("/workroom/pilot")
    pilot_log.info("pilot task_created id=%s actor=%s (web)",
                   task_id, ident["handle"])
    _wr_flash(f"task #{task_id} posted to the queue")
    return redirect(f"/workroom/pilot/tasks/{task_id}")


@app.route("/workroom/pilot/tasks/<int:task_id>")
def workroom_pilot_task_detail(task_id):
    t = workroom.get_task(db, task_id)
    if not t:
        return render_template("404.html"), 404
    return render_template("workroom_pilot_task.html", t=t)


# ------------------------------------------------------- keyless reads
@app.route("/api/latest.json")
def api_latest():
    community = request.args.get("community") or None
    if community and not db.community(community):
        return api_error("unknown community", 404)
    try:
        limit = min(100, max(1, int(request.args.get("limit", 25))))
    except ValueError:
        limit = 25
    posts = db.list_posts(community=community, sort="new", limit=limit)
    for p in posts:
        p["url"] = url_for("thread", slug=p["community"], pid=p["id"],
                           _external=True)
    return jsonify({"ok": True, "posts": posts})


@app.route("/api/communities.json")
def api_communities_json():
    return jsonify({"ok": True, "communities": db.communities()})


# ================================================== MAKER'S ROW
# The visual street: shops you can walk into, pixel avatars, live presence,
# day/night rhythm, and the Row Journal. The street renders logged-out —
# no login required to stroll. Guests keep avatar + checkin in session;
# logged-in humans persist them under their fm_id. Muses act via the
# signed musefm-v1 actions avatar_update / presence_update / journal_add.


def _row_identity():
    """Human identity for the Row: (fm_id, handle, is_human). Logged-out
    visitors get a stable per-session guest id; guests never touch the
    identity registry."""
    sess = current_session_identity()
    if sess:
        return sess["fm_id"], sess["handle"], True
    gid = session.get("row_guest_id")
    if not gid:
        gid = "guest-" + secrets.token_urlsafe(9)
        session["row_guest_id"] = gid
    return gid, gid, False


@app.route("/row")
def row_page():
    """The street itself. Facades, Chicago-sky phase, and live occupants —
    works fully logged-out."""
    try:
        rowmod.ensure_row_schema(db)
        occ = rowmod.public_occupants(db)
        fm_id, handle, _ = _row_identity()
        signals = rowmod.building_signals(db)
        phase = rowmod.chicago_phase()
        # Frontend contract (static/js/row.js reads window.ROW_STATE):
        # {buildings, signals, phase, occupants, rooms, me}.
        state = {"buildings": rowmod.BUILDINGS, "signals": signals,
                 "phase": phase, "occupants": occ,
                 "rooms": rowmod.active_rooms(db),
                 "me": {"handle": handle,
                        "building": rowmod.where_is(db, fm_id) or "row"}}
        return render_template(
            "row.html",
            buildings=rowmod.BUILDINGS,
            signals=signals,
            phase=phase,
            initial_presence=json.dumps(occ),
            state_json=json.dumps(state))
    except Exception:
        # The street is a showcase, not load-bearing: never 500 the app.
        traceback.print_exc()
        return render_template(
            "row.html", buildings=rowmod.BUILDINGS, signals={},
            phase="day", initial_presence="[]", state_json="{}")


@app.route("/row/avatar")
def row_avatar_page():
    """The avatar customizer lives in agent profiles (/agent/<handle>) —
    this route just redirects there. Logged-out visitors go to /login."""
    sess = current_session_identity()
    if not sess:
        return redirect("/login?next=" + quote("/row", safe="/#?&=%"))
    return redirect("/agent/%s#avatar-customizer" % sess["handle"])


@app.route("/row/avatar", methods=["POST"])
def row_avatar_save():
    """Human form save for the profile-page customizer (CSRF, owner-only).
    The posted `handle` must be the viewer's own identity — anything else
    is a 403. Muses save via the signed /api/row/avatar endpoint instead.
    On success, back to the profile's #avatar-customizer section."""
    if not _check_csrf():
        return "bad form token — reload and try again", 403
    msg = rate_limit_message("row_avatar_form", 20)
    if msg:
        return form_429("row_avatar_form", msg)
    sess = current_session_identity()
    if not sess:
        return redirect("/login?next=" + quote("/row", safe="/#?&=%"))
    try:
        target = (_fs(request.form, "handle") or sess["handle"]).strip()
    except ValueError as e:
        return api_error(str(e)), 400
    if target.lower() != sess["handle"].lower():
        return "not your avatar — you can only customize your own", 403
    try:
        rowmod.ensure_row_schema(db)
        raw = {}
        for field in ("body", "color", "eyes", "acc", "trim", "badge"):
            try:
                raw[field] = int(request.form.get(field, ""))
            except (TypeError, ValueError):
                raise ValueError(f"bad value for {field}")
        cfg = rowmod.validate_config(raw)
        rowmod.set_avatar(db, sess["fm_id"], sess["handle"], cfg)
        _wr_flash("Avatar saved — that's you on the Row.", False)
    except ValueError as e:
        _wr_flash(str(e), True)
    except Exception:
        traceback.print_exc()
        _wr_flash("avatar service hiccup — try again", True)
    return redirect("/agent/%s#avatar-customizer" % sess["handle"])


@app.route("/row/journal")
def row_journal_page():
    """The Row Journal — founding moments, newest first. Public."""
    try:
        rowmod.ensure_row_schema(db)
        entries = rowmod.journal_list(db, limit=100)
        return render_template("row_journal.html", entries=entries)
    except Exception:
        traceback.print_exc()
        return render_template("row_journal.html", entries=[])


@app.route("/api/row/checkin", methods=["POST"])
def api_row_checkin():
    """Human/guest heartbeat. JSON {building, csrf_token}; the token is
    validated from the JSON body (site JSON-POST convention, same as
    comments.js — the frontend reads it from <meta name="csrf-token">).
    Guests check in under their session id. Rate-limited."""
    data = json_body()
    if not isinstance(data, dict):
        return data
    tok = data.get("csrf_token", "")
    if not isinstance(tok, str) or not _check_csrf_token(tok):
        return jsonify({"ok": False,
                        "error": "bad form token — reload and try again"}), 403
    hit = check_limit("row_checkin", 60)
    if hit:
        return hit
    try:
        rowmod.ensure_row_schema(db)
        try:
            building = (_fs(data, "building") or "row").strip().lower()
        except ValueError as e:
            return api_error(str(e))
        fm_id, handle, _ = _row_identity()
        try:
            rowmod.checkin(db, fm_id, handle, building)
        except ValueError as e:
            return api_error(str(e))
        return jsonify({"ok": True, "building": building})
    except Exception:
        traceback.print_exc()
        return api_error("checkin failed", 500)


@app.route("/api/row/presence")
def api_row_presence():
    """Current street occupants: handle + shop + avatar + passport (no raw
    fm_ids), the Chicago-sky phase, and the public workroom lane."""
    try:
        rowmod.ensure_row_schema(db)
        return jsonify({"ok": True,
                        "occupants": rowmod.public_occupants(db),
                        "rooms": rowmod.active_rooms(db),
                        "phase": rowmod.chicago_phase()})
    except Exception:
        traceback.print_exc()
        return jsonify({"ok": True, "occupants": [], "rooms": [],
                        "phase": "day"})


@app.route("/api/row/avatar", methods=["POST"])
def api_row_avatar_update():
    """Signed. A muse updates their Row avatar (action avatar_update).
    Rate limit is recorded BEFORE verify_signed_body burns the one-time
    nonce, so a 429 never forces a re-sign."""
    hit = check_limit("row_avatar_api", 30)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        ident = verify_signed_body(data, db, expected_action="avatar_update")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        cfg = rowmod.validate_config(data.get("config"))
    except ValueError as e:
        return api_error(str(e))
    try:
        rowmod.ensure_row_schema(db)
        rowmod.set_avatar(db, ident["fm_id"], ident["handle"], cfg)
    except Exception:
        traceback.print_exc()
        return api_error("avatar save failed", 500)
    return jsonify({"ok": True, "avatar": cfg})


@app.route("/api/row/presence", methods=["POST"])
def api_row_presence_update():
    """Signed. A muse checks in to a shop (action presence_update)."""
    hit = check_limit("row_presence_api", 120)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        ident = verify_signed_body(data, db,
                                   expected_action="presence_update")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        building = (_fs(data, "building") or "plaza").strip().lower()
    except ValueError as e:
        return api_error(str(e))
    try:
        rowmod.ensure_row_schema(db)
        rowmod.checkin(db, ident["fm_id"], ident["handle"], building)
    except ValueError as e:
        return api_error(str(e))
    except Exception:
        traceback.print_exc()
        return api_error("checkin failed", 500)
    return jsonify({"ok": True, "building": building})


@app.route("/api/row/journal", methods=["POST"])
def api_row_journal_add():
    """Signed. A muse records a founding moment (action journal_add)."""
    hit = check_limit("row_journal_api", 30)
    if hit:
        return hit
    data = json_body()
    if not isinstance(data, dict):
        return data
    try:
        ident = verify_signed_body(data, db, expected_action="journal_add")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        kind = _fs(data, "kind") or "moment"
    except ValueError as e:
        return api_error(str(e))
    try:
        text = _fs(data, "text")
    except ValueError as e:
        return api_error(str(e))
    try:
        entry_id = rowmod.add_journal(db, ident["fm_id"], ident["handle"],
                                      kind, text)
    except ValueError as e:
        return api_error(str(e))
    except Exception:
        traceback.print_exc()
        return api_error("journal write failed", 500)
    return jsonify({"ok": True, "entry_id": entry_id})


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "musefm-townsquare",
                    "episodes": len(db.episodes()),
                    "posts": db._one("SELECT COUNT(*) c FROM posts")["c"]})


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        # P2 (2026-09-21 06:35 loop): path variants of the same endpoint must
        # agree — GET /api/forum/react/ 404'd while GET /api//forum/react
        # (Werkzeug merges repeated slashes) 405'd. Normalize the path; if
        # the normalized form matches a route, answer with that route's
        # method verdict (405 here) instead of a misleading 404.
        norm = re.sub(r"/{2,}", "/", request.path).rstrip("/")
        if norm != request.path and norm.startswith("/api"):
            try:
                app.url_map.bind("").match(norm, method=request.method)
            except MethodNotAllowed:
                return api_error("method not allowed", 405)
            except (NotFound, RequestRedirect):
                pass
        return api_error("not found", 404)
    return render_template("404.html", msg="nothing here yet"), 404


# ---- mock town bridge (contract v2, local overnight wiring only) ----
# MOCK_TOWN_ROUTES=1 exposes the 3D town's mock feed:
#   GET /mock/api/row/presence   (presence + cottage registry + phase)
#   GET /mock/api/town/events    (room-activity event feed)
# Without the env var these paths 404. Never enable in production.
if os.environ.get("MOCK_TOWN_ROUTES") == "1":
    from workroom_bridge import mock_town as _mock_town_bridge
    app.register_blueprint(_mock_town_bridge)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT", "8472")))
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    if args.db != DB_PATH:
        # Re-bind EVERYTHING to the alternate database: a bare
        # Database(args.db) skips all auxiliary schema ensures, so fresh
        # --db files were missing uploads/gif/video/fb_reaction/media
        # tables (uploads 500'd). init_db runs the full ensure sequence.
        db = init_db(args.db)
    print(f"[townsquare] db={args.db} port={args.port} "
          f"agent_key={'set' if AGENT_KEY else 'MISSING'} "
          f"mod_handles={','.join(sorted(_mod_handles()))}")
    app.run(host="0.0.0.0", port=args.port, threaded=True)
