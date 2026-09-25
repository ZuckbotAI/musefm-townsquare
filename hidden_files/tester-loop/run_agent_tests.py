#!/usr/bin/env python3
"""AGENT TESTER QA run — musefm-v1 signed machine-API flows against the LOCAL instance.
Writes freely to the scratch test DB. Never touches production.
Results: prints PASS/FAIL per test; writes raw notes to agent-tester-<stamp>.md.
"""
import base64, hashlib, io, json, os, secrets, struct, sys, time

sys.path.insert(0, "/home/hatch/workspace/musefm-townsquare")
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

import identity as ident

BASE = "http://127.0.0.1:8473"
STAMP = "2026-09-20-0635"
NOTES = "/home/hatch/workspace/musefm-townsquare/hidden_files/tester-loop/agent-tester-%s.md" % STAMP

notes_lines = []
def note(s):
    notes_lines.append(s)

results = []  # (name, status, detail)  status: "pass"|"fail"|"skip"
def check(name, ok, detail=""):
    st = "skip" if ok is None else ("pass" if ok else "fail")
    results.append((name, st, detail))
    note("### %s\n- %s\n- detail: %s\n" % (name, st.upper(), detail or "(none)"))

def new_key():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    raw_priv = priv.private_bytes(
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).Encoding.Raw,
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).PrivateFormat.Raw,
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).NoEncryption())
    raw_pub = pub.public_bytes_raw()
    return ident.b64u_encode(raw_priv), ident.b64u_encode(raw_pub)

def register(handle, pub_b64, extra=None, retries=6):
    body = {"handle": handle, "public_key": pub_b64}
    if extra: body.update(extra)
    n = 0
    while True:
        r = requests.post(BASE + "/api/identity/register", json=body)
        try: d = r.json()
        except Exception: d = {"_raw": r.text[:200]}
        if r.status_code != 429 or n >= retries:
            return r, d
        time.sleep(20); n += 1

def post_json(path, body, retries=0):
    """POST with optional retry on 429 (shared bucket with sibling testers)."""
    r = requests.post(BASE + path, json=body)
    try: d = r.json()
    except Exception: d = {"_raw": r.text[:300]}
    n = 0
    while r.status_code == 429 and n < retries:
        time.sleep(20)
        r = requests.post(BASE + path, json=body)
        try: d = r.json()
        except Exception: d = {"_raw": r.text[:300]}
        n += 1
    return r, d

def signed_json(ident_rec, action, **fields):
    return ident.signed_body(ident_rec["priv"], action, ident_rec["fm_id"], **fields)

# Vote endpoint helpers: the /api/forum/post bucket is 5/hr per IP and shared
# with sibling testers, so signature-level checks run against /api/forum/vote
# (bucket 120/hr). verify_signed_body is identical on every signed endpoint.
VOTE = "/api/forum/vote"
def vb(**kw):
    kw.setdefault("post_id", 99999); kw.setdefault("direction", 1)
    return signed_json(A, "vote", **kw)

def tiny_gif():
    # minimal valid 1x1 GIF89a
    return (b"GIF89a" + bytes([1,0,1,0,0x80,0,0,0,0,0,255,255,255,33,249,4,1,0,0,0,0,44,0,0,0,0,1,0,1,0,0,2,2,68,1,0,59]))

def tiny_png():
    import zlib
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes([200, 100, 50])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

def tiny_mp4(min_size=4096):
    # ftyp box + moov box (empty payload); total >= 4096 so structure validation passes
    ftyp_payload = b"isom" + b"\x00\x00\x00\x00" + b"isom"
    ftyp = struct.pack(">I", 8 + len(ftyp_payload)) + b"ftyp" + ftyp_payload
    moov_size = min_size - len(ftyp)
    moov = struct.pack(">I", moov_size) + b"moov" + b"\x00" * (moov_size - 8)
    return ftyp + moov

def upload(path, ident_rec, file_field, fname, raw, retries=4, **signed_fields):
    h = hashlib.sha256(raw).hexdigest()
    fields = {"file_sha256": h}
    fields.update(signed_fields)
    body = ident.signed_body(ident_rec["priv"], "upload", ident_rec["fm_id"], **fields)
    # multipart: all form fields are strings
    form = {k: str(v) for k, v in body.items()}
    files = {file_field: (fname, raw, "application/octet-stream")}
    r = requests.post(BASE + path, data=form, files=files)
    n = 0
    while r.status_code == 429 and n < retries:
        time.sleep(20)
        # nonce is one-time: re-sign for the retry
        body = ident.signed_body(ident_rec["priv"], "upload", ident_rec["fm_id"], **fields)
        form = {k: str(v) for k, v in body.items()}
        files = {file_field: (fname, raw, "application/octet-stream")}
        r = requests.post(BASE + path, data=form, files=files)
        n += 1
    try: d = r.json()
    except Exception: d = {"_raw": r.text[:300]}
    return r, d, body

# ============================================================ setup identities
note("# AGENT TESTER run — %s\nBase: %s (local scratch instance)\n" % (STAMP, BASE))
git_log = os.popen("cd /home/hatch/workspace/musefm-townsquare && git log --oneline -1").read().strip()
note("commit under test: `%s`\n" % git_log)

priv_a, pub_a = new_key()
TAG = str(int(time.time()))[-10:]  # unique handles per run (DB persists between my reruns)
r, d = register("qaa" + TAG, pub_a)
check("identity register A", r.status_code == 200 and d.get("ok") and d.get("fm_id"),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:300]))
A = {"priv": priv_a, "pub": pub_a, "fm_id": d.get("fm_id"), "handle": "qaa" + TAG}

priv_b, pub_b = new_key()
r, d = register("qab" + TAG, pub_b)
check("identity register B", r.status_code == 200 and d.get("ok") and d.get("fm_id"),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:300]))
B = {"priv": priv_b, "pub": pub_b, "fm_id": d.get("fm_id"), "handle": "qab" + TAG}

# ---- registration validation
r, d = register("qabot_alpha", pub_a)
check("duplicate handle rejected", r.status_code == 400,
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
r, d = register("qabot_gamma", "not-a-key")
check("bad public key rejected", r.status_code == 400,
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
r, d = register("ab", pub_a)
check("too-short handle rejected", r.status_code == 400,
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

# ============================================================ happy path
r, d = post_json("/api/forum/post", signed_json(A, "post", community="lobby",
    title="Agent QA hello", body="first signed post from the agent tester"), retries=6)
post_id = d.get("id") or d.get("post_id")
if r.status_code == 429:
    check("signed post", None, "SKIP: post bucket 5/hr exhausted by parallel testers even after retries")
else:
    check("signed post", r.status_code == 200 and d.get("ok") and post_id,
          "status=%d body=%s" % (r.status_code, json.dumps(d)[:300]))

# attribution: author handle must come from the registry even if a "handle" field is spoofed
spoof = signed_json(A, "post", community="lobby", title="spoof attempt",
                    body="x", handle="someone_else")
r2, d2 = post_json("/api/forum/post", spoof)
author = None
if r2.status_code == 200:
    rr = requests.get(BASE + "/api/forum/post/%d" % (d2.get("id") or 0))
    try: author = rr.json().get("post", {}).get("handle")
    except Exception: author = "?"
if r2.status_code == 429:
    check("handle spoof ignored (attribution from registry)", None,
          "SKIP: post bucket 5/hr exhausted by parallel testers")
else:
    check("handle spoof ignored (attribution from registry)",
          r2.status_code == 200 and author == A["handle"],
          "status=%d author=%r expected %r" % (r2.status_code, author, A["handle"]))

r, d = post_json("/api/forum/comment", signed_json(A, "comment", post_id=post_id,
    body="signed comment here"))
check("signed comment", r.status_code == 200 and d.get("ok"),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

r, d = post_json("/api/forum/vote", signed_json(A, "vote", post_id=post_id, direction=1))
check("signed vote", r.status_code == 200 and d.get("ok"),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

r, d = post_json("/api/forum/react", signed_json(A, "react", post_id=post_id, emoji="🔥"))
check("signed react", r.status_code in (200,) and d.get("ok") is not False,
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

# GIF upload + attach
gif_raw = tiny_gif()
r, d, _ = upload("/api/upload/gif", A, "gif", "qa.gif", gif_raw)
check("signed GIF upload", r.status_code == 200 and d.get("ok") and d.get("gif_url"),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
gif_url = d.get("gif_url", "")
if gif_url:
    r, d = post_json("/api/forum/post", signed_json(A, "post", community="lobby",
        title="post with gif", body="attaching a gif", gif_url=gif_url))
    check("post with gif_url attached", r.status_code == 200 and d.get("ok"),
          "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
else:
    check("post with gif_url attached", False, "no gif_url from upload step")

# image upload with ai_generated attestation (signed)
png_raw = tiny_png()
r, d, _ = upload("/api/upload/image", A, "image", "qa.png", png_raw, ai_generated="True")
check("signed image upload ai_generated=True",
      r.status_code == 200 and d.get("ok") and d.get("ai_generated") is True
      and d.get("status") == "approved",
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:300]))
img_url = d.get("image_url", "")

# image upload without ai_generated -> pending (attestation default)
r, d, _ = upload("/api/upload/image", B, "image", "qb.png", png_raw)
check("image upload without ai_generated lands pending",
      r.status_code == 200 and d.get("ok") and d.get("status") == "pending",
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:300]))

# video upload with ai_generated + title/description provenance, short-form
mp4_raw = tiny_mp4()
r, d, _ = upload("/api/upload/video", A, "video", "qa.mp4", mp4_raw,
                 ai_generated="True", duration_secs="45",
                 title="QA clip", description="agent tester clip")
check("signed video upload (ai_generated, short-form)",
      r.status_code == 200 and d.get("ok") and d.get("ai_generated") is True
      and d.get("status") == "approved" and d.get("duration_secs") == 45,
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:300]))
video_url = d.get("video_url", "")
vid_id = d.get("id")

# video without ai_generated -> pending
r, d, _ = upload("/api/upload/video", B, "video", "qb.mp4", mp4_raw,
                 ai_generated="False", duration_secs="120")
check("video upload ai_generated=False lands pending",
      r.status_code == 200 and d.get("ok") and d.get("status") == "pending",
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:300]))

# attestation check: approved short video appears in /api/shorts; pending does not
r = requests.get(BASE + "/api/shorts", params={"limit": 50})
try:
    shorts = r.json()
    short_ids = [s.get("id") for s in (shorts.get("shorts") or shorts.get("items") or [])]
except Exception:
    short_ids = []
check("approved short video visible in /api/shorts",
      vid_id in short_ids,
      "status=%d vid_id=%r in %r" % (r.status_code, vid_id, short_ids[:10]))

# long-form (>180s) video must NOT be short-eligible
r, d, _ = upload("/api/upload/video", A, "video", "long.mp4", mp4_raw,
                 ai_generated="True", duration_secs="3600")
long_vid = d.get("id")
r = requests.get(BASE + "/api/shorts", params={"limit": 100})
try:
    shorts = r.json()
    short_ids = [s.get("id") for s in (shorts.get("shorts") or shorts.get("items") or [])]
except Exception:
    short_ids = []
check("long-form video excluded from /api/shorts",
      long_vid is not None and long_vid not in short_ids,
      "long_vid=%r in_shorts=%r" % (long_vid, long_vid in short_ids))

# duet: approved parent + short duet
if vid_id:
    r, d, _ = upload("/api/upload/video", A, "video", "duet.mp4", mp4_raw,
                     ai_generated="True", duration_secs="30", duet_of=str(vid_id))
    check("duet upload binds duet_of",
          r.status_code == 200 and d.get("ok") and d.get("duet_of") == vid_id,
          "status=%d body=%s" % (r.status_code, json.dumps(d)[:300]))
r, d, _ = upload("/api/upload/video", A, "video", "duetbad.mp4", mp4_raw,
                 ai_generated="True", duration_secs="30", duet_of="99999")
check("duet_of nonexistent id rejected",
      r.status_code in (400, 404),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

# post with video_url attached
if video_url:
    r, d = post_json("/api/forum/post", signed_json(A, "post", community="lobby",
        title="post with video", body="attaching a video", video_url=video_url))
    check("post with video_url attached", r.status_code == 200 and d.get("ok"),
          "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

# identity_update signed
r, d = post_json("/api/identity/update", signed_json(A, "identity_update",
    bio="QA bot bio", avatar_url="https://example.com/a.png"))
check("signed identity_update", r.status_code == 200 and d.get("ok"),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
r = requests.get(BASE + "/api/identity/%s" % A["fm_id"])
try: bio = r.json().get("identity", {}).get("bio")
except Exception: bio = "?"
check("identity_update persisted", bio == "QA bot bio",
      "bio=%r" % bio)

# agent profile + endorsement
r, d = post_json("/api/agents/profile", signed_json(A, "agent_profile",
    tagline="QA agent", bio="tests things", skills="qa, testing",
    available="True"))
check("signed agent_profile", r.status_code == 200 and d.get("ok"),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
r, d = post_json("/api/agents/endorse", signed_json(B, "agent_endorse",
    handle="qabot_alpha", skill="qa", note="solid tester"))
check("signed agent_endorse", r.status_code == 200 and d.get("ok"),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

# agent memory write/read/export (signed identity journal)
r, d = post_json("/api/memory", signed_json(A, "memory_write",
    kind="note", title="qa note", body="remember this"))
check("signed memory_write", r.status_code in (200, 201) and d.get("ok"),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
mq = ident.signed_body(A["priv"], "memory_read", A["fm_id"], kind="note", limit="10")
r = requests.get(BASE + "/api/memory", params=mq)
try: dj = r.json(); has = dj.get("ok") and len(dj.get("entries", [])) > 0
except Exception: dj, has = {"_raw": r.text[:200]}, False
check("signed memory_read returns entry", has,
      "status=%d body=%s" % (r.status_code, json.dumps(dj)[:300]))
mq = ident.signed_body(A["priv"], "memory_export", A["fm_id"])
r = requests.get(BASE + "/api/memory/export", params=mq)
try: dj = r.json(); okx = dj.get("ok") is True
except Exception: dj, okx = {"_raw": r.text[:200]}, False
check("signed memory_export", r.status_code == 200 and okx,
      "status=%d" % r.status_code)

# trustline status signed GET
mq = ident.signed_body(A["priv"], "trustline_status", A["fm_id"])
r = requests.get(BASE + "/api/trustline/status", params=mq)
try: dj = r.json(); okx = dj.get("ok") is True
except Exception: dj, okx = {"_raw": r.text[:200]}, False
check("signed trustline_status GET", r.status_code == 200 and okx,
      "status=%d body=%s" % (r.status_code, json.dumps(dj)[:200]))

# ---- platform-signed attestations verify server-side
r = requests.get(BASE + "/api/platform-key")
pub = r.json().get("public_key", "")
check("platform-key published", r.status_code == 200 and len(pub) > 10,
      "status=%d pub_prefix=%s" % (r.status_code, pub[:16]))
r = requests.get(BASE + "/api/signal/credential/%s" % A["fm_id"])
try:
    env = r.json()
    pad = lambda s: s + "=" * (-len(s) % 4)
    pubkey = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(pad(pub)))
    pubkey.verify(base64.urlsafe_b64decode(pad(env["signature"])),
                  json.dumps(env["payload"], sort_keys=True, separators=(",", ":")).encode())
    sig_ok = True
except Exception as e:
    sig_ok, env = False, {"_raw": r.text[:200]}
check("signal credential platform signature verifies",
      r.status_code == 200 and sig_ok,
      "status=%d sig_ok=%r" % (r.status_code, sig_ok))
r = requests.get(BASE + "/api/passport/%s" % A["fm_id"])
try:
    env = r.json()
    pad = lambda s: s + "=" * (-len(s) % 4)
    pubkey = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(pad(pub)))
    pubkey.verify(base64.urlsafe_b64decode(pad(env["signature"])),
                  json.dumps(env["payload"], sort_keys=True, separators=(",", ":")).encode())
    sig_ok = True
except Exception:
    sig_ok = False
check("passport platform signature verifies", r.status_code == 200 and sig_ok,
      "status=%d sig_ok=%r" % (r.status_code, sig_ok))

# ---- timestamp as int (not string) still verifies — client robustness
# (uses the vote endpoint's wide bucket; a valid sig passes verification
# even when the target doesn't exist, so 400 = verified, 401 = not)
body = vb()
body["timestamp"] = int(body["timestamp"])
sig = ident.sign_fields(A["priv"], "vote", A["fm_id"], body["timestamp"],
                        body["nonce"],
                        {k: v for k, v in body.items() if k != "signature"})
body["signature"] = sig
r, d = post_json(VOTE, body)
check("integer timestamp verifies (400=verified target-miss, 401=fail)",
      r.status_code in (200, 400),
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

# ============================================================ adversarial
# (VOTE/vb defined up top; see note there)
def adv(name, fn, expect_status, expect_frag=None):
    try:
        r, d = fn()
        body = json.dumps(d)[:250] if isinstance(d, dict) else str(d)[:250]
        if r.status_code == 429:
            # shared per-IP bucket with the parallel HUMAN/ADVERSARIAL
            # testers: inconclusive, not a failure of the app
            check(name, None, "SKIP: status=429 rate-limited by parallel testers")
            return
        ok = r.status_code == expect_status and (expect_frag is None or expect_frag in body)
        check(name, ok, "status=%d (want %d) body=%s" % (r.status_code, expect_status, body))
    except Exception as e:
        check(name, False, "exception: %r" % e)

# A1 tampered payload after signing
b = vb(); b["direction"] = -1  # tamper after signing
adv("A1 tampered payload rejected", lambda: post_json(VOTE, b), 401, "signature does not verify")

# A2 garbage signature
b = vb(); b["signature"] = "AAAA"
adv("A2 invalid signature rejected", lambda: post_json(VOTE, b), 401)

# A3 replayed nonce (valid sig; target may not exist -> 400 first, nonce still burned)
b = vb()
r1, d1 = post_json(VOTE, b)
r2, d2 = post_json(VOTE, b)
check("A3 replayed nonce rejected (first consumed, second replay)",
      r1.status_code in (200, 400) and r2.status_code == 401 and "replay" in json.dumps(d2),
      "first=%d second=%d body=%s" % (r1.status_code, r2.status_code, json.dumps(d2)[:200]))

# A4 malformed JSON
r = requests.post(BASE + VOTE, data="this is not json",
                  headers={"Content-Type": "application/json"})
check("A4 malformed JSON -> 400", r.status_code == 400,
      "status=%d body=%s" % (r.status_code, r.text[:200]))

# A5 non-object JSON
r = requests.post(BASE + VOTE, json=[1, 2, 3])
check("A5 non-object JSON -> 400", r.status_code == 400,
      "status=%d body=%s" % (r.status_code, r.text[:200]))

# A6 missing everything
adv("A6 empty body rejected", lambda: post_json(VOTE, {}), 401)

# A7 wrong action for endpoint (signed "comment" body at the vote endpoint)
b = signed_json(A, "comment", post_id=99999, body="wrong action here")
adv("A7 wrong action for endpoint rejected",
    lambda: post_json(VOTE, b), 401, "wrong action")

# A8 stale timestamp (10 min old)
ts = str(int(time.time() * 1000) - 10 * 60 * 1000)
nonce = ident.new_nonce()
fields = {"action": "vote", "post_id": 99999, "direction": 1}
sig = ident.sign_fields(A["priv"], "vote", A["fm_id"], ts, nonce, fields)
b = {"action": "vote", "fm_id": A["fm_id"], "timestamp": ts, "nonce": nonce,
     "signature": sig, "post_id": 99999, "direction": 1}
adv("A8 stale timestamp rejected", lambda: post_json(VOTE, b), 401, "window")

# A9 future timestamp (+10 min)
ts = str(int(time.time() * 1000) + 10 * 60 * 1000)
nonce = ident.new_nonce()
sig = ident.sign_fields(A["priv"], "vote", A["fm_id"], ts, nonce, fields)
b = {"action": "vote", "fm_id": A["fm_id"], "timestamp": ts, "nonce": nonce,
     "signature": sig, "post_id": 99999, "direction": 1}
adv("A9 future timestamp rejected", lambda: post_json(VOTE, b), 401, "window")

# A10 unknown fm_id (valid sig, unregistered key)
priv_u, pub_u = new_key()
fm_u = "fm_" + ident.b64u_encode(secrets.token_bytes(9))
b = ident.signed_body(priv_u, "vote", fm_u, post_id=99999, direction=1)
adv("A10 unknown fm_id rejected", lambda: post_json(VOTE, b), 401, "unknown fm_id")

# A11 bad nonce length (8 bytes)
b = vb()
nonce8 = ident.b64u_encode(secrets.token_bytes(8))
b2 = dict(b); b2["nonce"] = nonce8
b2["signature"] = ident.sign_fields(A["priv"], "vote", A["fm_id"], b2["timestamp"],
                                    nonce8, {k: v for k, v in b2.items() if k != "signature"})
adv("A11 bad nonce length rejected", lambda: post_json(VOTE, b2), 401, "nonce")

# A12 empty POST to signed-only endpoint
r = requests.post(BASE + "/api/forum/vote")
check("A12 empty POST to signed endpoint -> 401",
      r.status_code == 401, "status=%d body=%s" % (r.status_code, r.text[:200]))

# A13 unsigned multipart upload
r = requests.post(BASE + "/api/upload/gif",
                  files={"gif": ("q.gif", gif_raw, "image/gif")})
check("A13 unsigned multipart upload rejected",
      r.status_code == 401, "status=%d body=%s" % (r.status_code, r.text[:200]))

# A14 sha256 mismatch: sign hash of file X, upload file Y
r, d, _ = upload("/api/upload/gif", A, "gif", "qa.gif", gif_raw)  # baseline ok
other = tiny_gif() + b"\x00"
h_wrong = hashlib.sha256(other).hexdigest()
body = ident.signed_body(A["priv"], "upload", A["fm_id"], file_sha256=h_wrong)
form = {k: str(v) for k, v in body.items()}
r = requests.post(BASE + "/api/upload/gif", data=form,
                  files={"gif": ("q.gif", gif_raw, "image/gif")})
try: dj = r.json()
except Exception: dj = {"_raw": r.text[:200]}
check("A14 sha256 mismatch rejected",
      r.status_code == 401 and "file_sha256" in json.dumps(dj),
      "status=%d body=%s" % (r.status_code, json.dumps(dj)[:200]))

# A15 ai_generated flip in transit (signed False, sent True)
body = ident.signed_body(A["priv"], "upload", A["fm_id"],
                         file_sha256=hashlib.sha256(mp4_raw).hexdigest(),
                         ai_generated="False", duration_secs="30")
form = {k: str(v) for k, v in body.items()}
form["ai_generated"] = "True"  # tamper after signing
r = requests.post(BASE + "/api/upload/video", data=form,
                  files={"video": ("q.mp4", mp4_raw, "video/mp4")})
check("A15 ai_generated flip in transit rejected",
      r.status_code == 401, "status=%d body=%s" % (r.status_code, r.text[:200]))

# A16 cross-identity: A's key with B's fm_id
b = vb()
b["fm_id"] = B["fm_id"]
adv("A16 signature/key mismatch (A key, B fm_id) rejected",
    lambda: post_json(VOTE, b), 401, "signature does not verify")

# A17 cross-endpoint replay: valid signed "comment" body sent to vote endpoint
b = signed_json(A, "comment", post_id=99999, body="endpoint hop")
adv("A17 signed body replayed at wrong endpoint rejected",
    lambda: post_json(VOTE, b), 401, "wrong action")

# A18 video title tamper in transit
body = ident.signed_body(A["priv"], "upload", A["fm_id"],
                         file_sha256=hashlib.sha256(mp4_raw).hexdigest(),
                         ai_generated="True", duration_secs="30",
                         title="real title")
form = {k: str(v) for k, v in body.items()}
form["title"] = "FAKE TITLE"
r = requests.post(BASE + "/api/upload/video", data=form,
                  files={"video": ("q.mp4", mp4_raw, "video/mp4")})
check("A18 video title tamper in transit rejected",
      r.status_code == 401, "status=%d body=%s" % (r.status_code, r.text[:200]))

# A19 garbage nonce b64
b = vb(); b["nonce"] = "!!!"
adv("A19 garbage nonce rejected", lambda: post_json(VOTE, b), 401, "nonce")

# A20 tampered signed GET query (trustline_status with flipped handle-free field tamper: change fm_id)
mq = ident.signed_body(A["priv"], "trustline_status", A["fm_id"])
mq["fm_id"] = B["fm_id"]
r = requests.get(BASE + "/api/trustline/status", params=mq)
check("A20 tampered signed GET rejected",
      r.status_code == 401, "status=%d body=%s" % (r.status_code, r.text[:200]))

# A21 missing signature only
b = vb(); del b["signature"]
adv("A21 missing signature rejected", lambda: post_json(VOTE, b), 401)

# A22 shared agent-key path: valid key posts as given handle
agent_key = None
try:
    with open("/home/hatch/workspace/musefm-townsquare/.agent_key") as fh:
        agent_key = fh.read().strip()
except Exception:
    agent_key = None
if agent_key:
    r, d = post_json("/api/forum/post",
                     {"agent_key": agent_key, "handle": "keybot",
                      "community": "lobby", "title": "agent key post", "body": "via shared key"},
                     retries=3)
    if r.status_code == 429:
        check("A22 shared agent key posts with given handle", None,
              "SKIP: post bucket 5/hr exhausted by parallel testers")
    else:
        check("A22 shared agent key posts with given handle",
              r.status_code == 200 and d.get("ok"),
              "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
    r, d = post_json("/api/forum/post",
                     {"agent_key": "wrong-key", "handle": "keybot",
                      "community": "lobby", "title": "t", "body": "x"})
    if r.status_code == 429:
        check("A23 wrong agent key rejected", None,
              "SKIP: post bucket 5/hr exhausted by parallel testers")
    else:
        check("A23 wrong agent key rejected", r.status_code == 401,
              "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
    # agent key on a signed-ONLY endpoint (memory write requires signed identity)
    r, d = post_json("/api/memory",
                     {"agent_key": agent_key, "action": "memory_write",
                      "kind": "note", "title": "t", "body": "x"})
    check("A24 agent key cannot bypass signed-only memory_write",
          r.status_code in (401, 400),
          "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
else:
    check("A22 shared agent key posts with given handle", False, "no .agent_key file found")

# A22b/A23b: shared agent-key path exercised via the vote endpoint
# (identical require_agent_or_signature wrapper as /api/forum/post)
if agent_key:
    r, d = post_json(VOTE, {"agent_key": agent_key, "handle": "keybot",
                            "post_id": 99999, "direction": 1})
    if r.status_code == 429:
        check("A22b agent key accepted on signed endpoint", None,
              "SKIP: vote bucket hot")
    else:
        # valid key passes auth; target 99999 doesn't exist -> 400, not 401
        check("A22b agent key accepted on signed endpoint",
              r.status_code == 400 and "unknown target" in json.dumps(d),
              "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))
    r, d = post_json(VOTE, {"agent_key": "wrong-key", "handle": "keybot",
                            "post_id": 99999, "direction": 1})
    check("A23b wrong agent key rejected", r.status_code == 401,
          "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

# A25 unsigned hit on identity_update (signed-only)
r, d = post_json("/api/identity/update", {"action": "identity_update", "bio": "hax"})
check("A25 unsigned identity_update rejected", r.status_code == 401,
      "status=%d body=%s" % (r.status_code, json.dumps(d)[:200]))

# ============================================================ summary
fails = [n for n, st, _ in results if st == "fail"]
skips = [n for n, st, _ in results if st == "skip"]
passes = [n for n, st, _ in results if st == "pass"]
note("\n## Summary\n- total: %d, pass: %d, fail: %d, skip: %d\n" %
     (len(results), len(passes), len(fails), len(skips)))
with open(NOTES, "w") as fh:
    fh.write("\n".join(notes_lines))

print("TOTAL=%d PASS=%d FAIL=%d SKIP=%d" % (len(results), len(passes), len(fails), len(skips)))
for n, st, det in results:
    if st == "fail":
        print("FAIL:", n, "|", det[:220])
    elif st == "skip":
        print("SKIP:", n, "|", det[:220])
print("notes ->", NOTES)
