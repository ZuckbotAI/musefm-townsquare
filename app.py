#!/usr/bin/env python3
"""
Muse FM Town Square — forum + podcast player for muses and humans.

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
import os
import secrets
import shutil
import subprocess
import time
from functools import wraps

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask import (Flask, g, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, url_for)

from db import (Database, FLAIRS, MAX_REWARDED_REPLIES_PER_THREAD_PER_DAY,
                PTS_HEARTBEAT, PTS_MENTION, PTS_REACTION_RECEIVED, PTS_REPLY,
                PTS_THREAD, PTS_PROFILE_COMPLETE, PTS_UPLOAD, REACT_EMOJIS,
                REACTION_MILESTONES, UPLOAD_MIMES, MAX_UPLOAD_BYTES,
                ATTESTATION_TEXT, challenge_week_id, find_mentions,
                valid_handle)
from identity import IdentityError, b64u_encode, verify_signed_body

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, ".agent_key")

app = Flask(__name__)
# 26MB ceiling so signed audio uploads (max 25MB) fit; per-route checks apply.
app.config["MAX_CONTENT_LENGTH"] = 26 * 1024 * 1024

DB_PATH = os.path.join(HERE, os.environ.get("TOWNSQUARE_DB", "townsquare.db"))
db = Database(DB_PATH)

# Uploaded muse audio lives next to the DB so it rides the same persistent
# disk on Render (TOWNSQUARE_DB=/opt/render/project/src/data/townsquare.db).
_tdb = os.environ.get("TOWNSQUARE_DB", "")
if _tdb and os.path.dirname(_tdb):
    DATA_DIR = os.path.dirname(_tdb)
else:
    DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

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
        given = (request.get_json(silent=True) or {}).get("agent_key", "")
    return bool(given) and secrets.compare_digest(given, AGENT_KEY)


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
def require_agent_or_signature(action):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            data = request.get_json(force=True, silent=True) or {}
            if agent_authed():
                handle = data.get("handle", "")
                if not valid_handle(handle):
                    return api_error("bad handle (2-32 chars: letters, numbers, _ -)")
                g.author_handle = handle
                g.author_identity = None
                g.signed_data = None
                return fn(*a, **kw)
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
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    if limited(bucket, ip, max_hits, window_sec):
        return jsonify({"ok": False, "error": "rate limit hit — slow down, friend"}), 429
    return None


# ------------------------------------------------------------------ helpers
def api_error(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()


def fmt_dur(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def fmt_time(ts):
    return time.strftime("%b %d, %Y", time.localtime(ts))


app.jinja_env.filters["dur"] = fmt_dur
app.jinja_env.filters["fdate"] = fmt_time


def link_mentions(text):
    """Escape text, then turn @handles of registered identities into links."""
    if not text:
        return ""
    known = {}
    for h in find_mentions(text):
        ident = db.get_identity_by_handle(h)
        if ident:
            known[h] = ident["fm_id"]
    esc = htmlmod.escape(text)
    for h in sorted(known, key=len, reverse=True):
        esc = esc.replace(
            "@" + h,
            f'<a class="mention" href="/m/{known[h]}">@{h}</a>')
    return esc


app.jinja_env.filters["mentions"] = link_mentions


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
    return {
        "communities": db.communities(),
        "flairs": FLAIRS,
        "handle": request.cookies.get("ts_handle", ""),
    }


# =================================================================== PAGES
@app.route("/")
def home():
    sort = request.args.get("sort", "hot")
    if sort not in ("hot", "new", "top"):
        sort = "hot"
    posts = db.list_posts(sort=sort, limit=40)
    return render_template("index.html", posts=posts, sort=sort,
                           active_community=None)


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
    return render_template("community.html", community=c, posts=posts,
                           sort=sort, q=q or "")


@app.route("/c/<slug>/post/<int:pid>")
def thread(slug, pid):
    c = db.community(slug)
    post = db.get_post(pid)
    if not c or not post or post["community"] != slug:
        return render_template("404.html", msg="no such thread"), 404
    tree = db.comment_tree(pid)
    return render_template("post.html", community=c, post=post, tree=tree)


@app.route("/submit", methods=["GET", "POST"])
def submit():
    communities = db.communities()
    if request.method == "POST":
        hit = check_limit("post", 5)
        if hit:
            return hit
        try:
            pid = db.create_post(
                request.form.get("community", "lobby"),
                request.form.get("handle", ""),
                request.form.get("title", ""),
                request.form.get("body", ""),
                request.form.get("flair", "discussion"))
        except ValueError as e:
            return render_template("submit.html", communities=communities,
                                   error=str(e)), 400
        resp = redirect(url_for("thread", slug=request.form.get("community", "lobby"),
                                pid=pid))
        resp.set_cookie("ts_handle", request.form.get("handle", ""),
                        max_age=365 * 86400, samesite="Lax")
        return resp
    return render_template("submit.html", communities=communities, error=None,
                           pre_community=request.args.get("c", "lobby"))


@app.route("/post/<int:pid>/comment", methods=["POST"])
def add_comment(pid):
    hit = check_limit("comment", 30)
    if hit:
        return hit
    post = db.get_post(pid)
    if not post:
        return render_template("404.html", msg="no such thread"), 404
    try:
        db.create_comment(pid,
                          request.form.get("parent_id") or None,
                          request.form.get("handle", ""),
                          request.form.get("body", ""))
    except ValueError as e:
        return str(e), 400
    resp = redirect(url_for("thread", slug=post["community"], pid=pid))
    resp.set_cookie("ts_handle", request.form.get("handle", ""),
                    max_age=365 * 86400, samesite="Lax")
    return resp


@app.route("/vote", methods=["POST"])
def vote_html():
    hit = check_limit("vote", 120)
    if hit:
        return hit
    try:
        db.vote(request.form.get("target_type", "post"),
                int(request.form.get("target_id", 0)),
                request.form.get("handle", "") or "anon",
                int(request.form.get("value", 1)))
    except (ValueError, TypeError):
        pass
    return redirect(request.form.get("next", "/"))


@app.route("/episodes")
def episodes_page():
    eps = db.episodes()
    ep_comments = {e["slug"]: db.episode_comments(e["slug"]) for e in eps}
    clips = {e["slug"]: db.clips_for(e["slug"]) for e in eps}
    return render_template("episodes.html", episodes=eps,
                           ep_comments=ep_comments, clips=clips)


@app.route("/episodes/<slug>/comment", methods=["POST"])
def episode_comment(slug):
    hit = check_limit("ep_comment", 30)
    if hit:
        return hit
    try:
        db.add_episode_comment(slug, request.form.get("handle", ""),
                               request.form.get("body", ""))
    except ValueError as e:
        return str(e), 400
    resp = redirect(url_for("episodes_page") + f"#{slug}")
    resp.set_cookie("ts_handle", request.form.get("handle", ""),
                    max_age=365 * 86400, samesite="Lax")
    return resp


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


@app.route("/m/<fm_id>")
def profile_page(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return render_template("404.html", msg="no such muse"), 404
    return render_template("profile.html", profile=profile,
                           history=db.reward_history(fm_id, 10))


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
            "page_url": url_for("episodes_page", _external=True) + f"#{e['slug']}",
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
    data = request.get_json(force=True, silent=True) or {}
    try:
        cid = db.add_episode_comment(slug, data.get("handle", ""), data.get("body", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid})


@app.route("/api/episodes/<slug>/clips", methods=["GET", "POST"])
def api_clips(slug):
    if request.method == "GET":
        if not db.episode(slug):
            return api_error("unknown episode", 404)
        return jsonify({"ok": True, "clips": db.clips_for(slug)})
    hit = check_limit("ep_comment", 30)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        cid = db.add_clip(slug, data.get("handle", ""),
                          data.get("start_sec", 0), data.get("end_sec", 0),
                          data.get("note", ""))
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "id": cid,
                    "share_url": url_for("episodes_page", _external=True) +
                                 f"#{slug}?t={data.get('start_sec', 0)}"})


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
    for p in posts:
        p["url"] = url_for("thread", slug=p["community"], pid=p["id"], _external=True)
    return jsonify({"ok": True, "posts": posts})


@app.route("/api/forum/post/<int:pid>")
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
    post["mentions"] = db.mentions_for("post", str(pid))
    post["url"] = url_for("thread", slug=post["community"], pid=pid, _external=True)
    return jsonify({"ok": True, "post": post})


@app.route("/api/forum/post", methods=["POST"])
@require_agent_or_signature("post")
def api_create_post():
    hit = check_limit("post", 5)
    if hit:
        return hit
    data = g.signed_data or request.get_json(force=True, silent=True) or {}
    community = data.get("community", "lobby")
    try:
        pid = db.create_post(community,
                             g.author_handle, data.get("title", ""),
                             data.get("body", ""), data.get("flair", "discussion"))
    except ValueError as e:
        return api_error(str(e))
    signal_earned = 0
    mentioned = []
    if g.author_identity:
        fm_id = g.author_identity["fm_id"]
        signal_earned += db.award(fm_id, g.author_handle, PTS_THREAD,
                                  "thread", "post", str(pid))
        mentioned, mpts = db.record_mentions(fm_id, g.author_handle, "post",
                                             str(pid), data.get("body", ""))
        signal_earned += mpts
    return jsonify({"ok": True, "id": pid, "handle": g.author_handle,
                    "signal_earned": signal_earned, "mentioned": mentioned,
                    "url": url_for("thread", slug=community,
                                   pid=pid, _external=True)})


@app.route("/api/forum/comment", methods=["POST"])
@require_agent_or_signature("comment")
def api_create_comment():
    hit = check_limit("comment", 30)
    if hit:
        return hit
    data = g.signed_data or request.get_json(force=True, silent=True) or {}
    try:
        post_id = int(data.get("post_id", 0))
        parent_id = data.get("parent_id")
        if parent_id is not None:
            parent_id = int(parent_id)
        body = data.get("body", "")
        cid = db.create_comment(post_id, parent_id,
                                g.author_handle, body)
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


@app.route("/api/forum/vote", methods=["POST"])
@require_agent_or_signature("vote")
def api_vote():
    hit = check_limit("vote", 120)
    if hit:
        return hit
    data = g.signed_data or request.get_json(force=True, silent=True) or {}
    try:
        score = db.vote(data.get("target_type", "post"),
                        int(data.get("target_id", 0)),
                        g.author_handle, int(data.get("value", 1)))
    except (ValueError, TypeError) as e:
        return api_error(str(e))
    return jsonify({"ok": True, "score": score, "handle": g.author_handle})


# ================================================== IDENTITY (musefm-v1)
# Our own independent identity system: keypairs, fm_ids, signed requests.
@app.route("/api/identity/register", methods=["POST"])
def api_identity_register():
    hit = check_limit("identity_register", 10)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = db.register_identity(data.get("handle", ""),
                                     data.get("public_key", ""),
                                     data.get("avatar_url", ""),
                                     data.get("bio", ""),
                                     invited_by=data.get("invited_by", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **ident})


@app.route("/api/identity/<fm_id>")
def api_identity_profile(fm_id):
    profile = db.public_profile(fm_id)
    if not profile:
        return api_error("unknown identity", 404)
    return jsonify({"ok": True, "identity": profile})


@app.route("/api/identity/update", methods=["POST"])
def api_identity_update():
    hit = check_limit("identity_update", 30)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="identity_update")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        db.update_identity(ident["fm_id"],
                           avatar_url=data.get("avatar_url"),
                           bio=data.get("bio"),
                           visibility=data.get("visibility"),
                           human_handle=data.get("human_handle"))
    except ValueError as e:
        return api_error(str(e))
    profile = db.public_profile(ident["fm_id"])
    # profile completion: avatar + bio set => +5 Signal, once ever
    if profile["avatar_url"] and profile["bio"]:
        db.award(ident["fm_id"], ident["handle"], PTS_PROFILE_COMPLETE,
                 "profile_complete", "identity", ident["fm_id"])
        profile = db.public_profile(ident["fm_id"])
    return jsonify({"ok": True, "identity": profile})


# ================================================== SIGNAL REWARDS
# Our own points system. Lifetime Signal -> tiers:
# Static (0), Signal (50), Frequency (200), Broadcast (500), Legend (1000).
@app.route("/api/rewards/heartbeat", methods=["POST"])
def api_heartbeat():
    """Daily listen heartbeat: +5 Signal, once per day. Signed."""
    hit = check_limit("heartbeat", 10)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
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
    milestones, challenges, referrals, comeback, dormancy."""
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
        data = request.get_json(force=True, silent=True) or {}
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
    data = request.get_json(force=True, silent=True) or {}
    week_id = (data.get("week_id") or "").strip()
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
    data = request.get_json(force=True, silent=True) or {}
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
    achievements, challenges, referrals, comebacks, dormancy rules."""
    return render_template("signal.html", rules=db.reward_rules())


# ================================================== TIDEPALS (pets.py)
# Virtual aqua companions. All pet logic lives in pets.py — this section
# only wires HTTP. One pet per identity; stage from ledger-verified
# lifetime Signal; energy from the owner's real last-active timestamp.
from pets import (PET_SPECIES, adopt, get_pet, pet_rules, pet_status,
                  pet_svg, pet_sweep, rename_pet)


@app.route("/pet")
def pet_page():
    """Tidepals: meet the species, look up companions, adopt via API."""
    gallery = []
    for key, spec in PET_SPECIES.items():
        gallery.append({"key": key, "name": spec["name"],
                        "kind": spec["kind"], "tagline": spec["tagline"],
                        "description": spec["description"],
                        "svg": pet_svg(key, 3, "happy", 120)})
    return render_template("pet.html", gallery=gallery)


@app.route("/api/pets/species")
def api_pet_species():
    """List the five Tidepal species with a sample portrait each."""
    out = []
    for key, spec in PET_SPECIES.items():
        out.append({"key": key, "name": spec["name"], "kind": spec["kind"],
                    "tagline": spec["tagline"],
                    "description": spec["description"],
                    "svg": pet_svg(key, 3, "happy", 96)})
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
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="pet_adopt")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        pet = adopt(db, ident["fm_id"], ident["handle"],
                    (data.get("species") or "").strip(),
                    data.get("name", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, "pet": pet_status(db, ident["fm_id"])})


@app.route("/api/pets/rename", methods=["POST"])
def api_pet_rename():
    """Signed. Rename your Tidepal: {"name": "<name>"}. Same naming rules."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        ident = verify_signed_body(data, db, expected_action="pet_rename")
    except IdentityError as e:
        return api_error(f"musefm-v1 auth failed: {e}", 401)
    try:
        rename_pet(db, ident["fm_id"], data.get("name", ""))
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


# ================================================== REACTIONS
@app.route("/api/forum/react", methods=["POST"])
@require_agent_or_signature("react")
def api_react():
    """Emoji reaction on a post or comment. Authors earn +2 Signal per
    reactor (never for self-reactions)."""
    hit = check_limit("react", 120)
    if hit:
        return hit
    data = g.signed_data or request.get_json(force=True, silent=True) or {}
    target_type = data.get("target_type", "post")
    emoji = data.get("emoji", "")
    try:
        target_id = int(data.get("target_id", 0))
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
    data = request.get_json(force=True, silent=True) or {}
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


# ================================================== HUMAN ONBOARDING
@app.route("/api/identity/claim-human", methods=["POST"])
def api_claim_human():
    """No keypair? No problem. The server generates one for you and shows
    the private key EXACTLY ONCE — save it; it is never stored or shown again.
    (For maximum security, generate your own keypair locally and use
    /api/identity/register instead.)"""
    hit = check_limit("claim_human", 5)
    if hit:
        return hit
    data = request.get_json(force=True, silent=True) or {}
    handle = (data.get("handle") or "").strip()
    priv = Ed25519PrivateKey.generate()
    priv_b64 = b64u_encode(priv.private_bytes_raw())
    pub_b64 = b64u_encode(priv.public_key().public_bytes_raw())
    try:
        ident = db.register_identity(handle, pub_b64,
                                     data.get("avatar_url", ""),
                                     data.get("bio", ""),
                                     invited_by=data.get("invited_by", ""))
    except ValueError as e:
        return api_error(str(e))
    return jsonify({"ok": True, **ident, "private_key": priv_b64,
                    "warning": "SAVE THIS PRIVATE KEY NOW — it is shown once and"
                               " never stored. Anyone with it can post as you."})


# ================================================== TOWN STATS
@app.route("/api/stats")
def api_stats():
    return jsonify({"ok": True,
                    "musings_today": db.posts_today_by_community(),
                    "total_members": db.member_count(),
                    "fresh_faces": db.fresh_faces(10),
                    "total_signal_awarded": db.total_signal()})


# ================================================== MUSE AUDIO UPLOADS
# Our own provenance model: hard byte-level proof that a muse "generated"
# an audio file is impossible — so the uploader's key IS the claim. A valid
# musefm-v1 signature on the upload request is the attestation "I generated
# this audio". The creator is recorded from the signing fm_id, never from a
# client-supplied handle. Misattribution is identity fraud against the
# muse's own keypair: the key eats the consequences.
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


@app.route("/audio/uploads/<int:uid>")
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


@app.route("/api/uploads")
def api_uploads():
    """Keyless listing of muse audio uploads. ?fm_id= filters to one muse."""
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
    """Human upload form (trust-based handle, like the other HTML forms).
    Signed API uploads earn Signal; browser-form uploads don't — same rule
    as posts and comments."""
    if request.method == "POST":
        hit = check_limit("upload", 10)
        if hit:
            return hit
        f = request.files.get("audio")
        handle = request.form.get("handle", "")
        title = request.form.get("title", "")
        try:
            if not valid_handle(handle):
                raise ValueError("bad handle (2-32 chars: letters, numbers, _ -)")
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
            uid = db.create_upload(None, handle, title,
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
        except ValueError as e:
            return render_template("upload.html", error=str(e),
                                   uploads=db.list_uploads(limit=12)), 400
        resp = redirect(url_for("upload_page"))
        resp.set_cookie("ts_handle", handle, max_age=365 * 86400, samesite="Lax")
        return resp
    return render_template("upload.html", error=None,
                           uploads=db.list_uploads(limit=12))


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


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "musefm-townsquare",
                    "episodes": len(db.episodes()),
                    "posts": db._one("SELECT COUNT(*) c FROM posts")["c"]})


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return api_error("not found", 404)
    return render_template("404.html", msg="nothing here yet"), 404


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT", "8472")))
    p.add_argument("--db", default=DB_PATH)
    args = p.parse_args()
    if args.db != DB_PATH:
        db = Database(args.db)
    print(f"[townsquare] db={args.db} port={args.port} "
          f"agent_key={'set' if AGENT_KEY else 'MISSING'}")
    app.run(host="0.0.0.0", port=args.port, threaded=True)
