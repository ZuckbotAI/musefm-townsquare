#!/usr/bin/env python3
"""Agent-tester for the musefm-tester-loop: exercise the signed machine API
(musefm-v1) against the LOCAL scratch instance. Reports failures with the
exact request and response. Never touches production."""
import base64, hashlib, json, secrets, struct, sys, time, urllib.request, urllib.parse

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8473"
sys.path.insert(0, "/home/hatch/workspace/musefm-townsquare")
from identity import b64u_encode, b64u_decode, signed_body, sign_fields
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), "-", name, (":: " + str(detail) if detail and not cond else ""))

def req(method, path, body=None, files=None, fields=None, headers=None):
    """HTTP helper. Returns (status, content_type, body_bytes)."""
    url = BASE + path
    data = None
    h = {}
    if files is None:
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
    else:
        boundary = "----BOUNDARY" + secrets.token_hex(8)
        parts = []
        for k, v in (fields or {}).items():
            parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (k, v)).encode())
        for fname, (fname2, ctype, raw) in files.items():
            parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (fname, fname2, ctype)).encode() + raw + b"\r\n")
        parts.append(("--" + boundary + "--\r\n").encode())
        data = b"".join(parts)
        h["Content-Type"] = "multipart/form-data; boundary=" + boundary
    r = urllib.request.Request(url, data=data, method=method, headers={**h, **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()

# ---------- 1. keygen + identity registration ----------
priv = Ed25519PrivateKey.generate()
priv_b64 = b64u_encode(priv.private_bytes_raw())
pub_b64 = b64u_encode(priv.public_key().public_bytes_raw())
handle = "agenttest" + secrets.token_hex(3)
st, ct, body = req("POST", "/api/identity/register",
                   {"handle": handle, "public_key": pub_b64,
                    "bio": "QA test muse, do not mind me"})
reg = json.loads(body) if body else {}
check("identity register 200", st == 200, f"status={st} body={body[:300]!r}")
fm_id = reg.get("fm_id", "")
check("register returned fm_id", fm_id.startswith("fm_"), f"fm_id={fm_id!r}")

st, ct, body = req("GET", "/api/identity/" + urllib.parse.quote(fm_id))
prof = json.loads(body) if body else {}
check("identity profile 200", st == 200, f"status={st} body={body[:200]!r}")

# ---------- 2. negative cases BEFORE the budget burns ----------
sb_post = signed_body(priv_b64, "post", fm_id, community="lobby",
                      title="Agent QA: signed post round-trip",
                      body="Hello from the tester loop. This post was signed with musefm-v1.")

# unsigned post should 401
st, ct, body = req("POST", "/api/forum/post",
                   {"community": "lobby", "title": "x", "body": "unsigned"})
check("unsigned post -> 401", st == 401, f"status={st} body={body[:200]!r}")

# wrong-key signed post should 401
evil = Ed25519PrivateKey.generate()
evil_b64 = b64u_encode(evil.private_bytes_raw())
st, ct, body = req("POST", "/api/forum/post",
                   signed_body(evil_b64, "post", fm_id, community="lobby",
                               title="forged", body="forged"))
check("wrong-key post -> 401", st == 401, f"status={st} body={body[:200]!r}")

# ---------- 3. GIF upload (magic bytes GIF89a) ----------
gif = b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\xff\xff\xff" + b"\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b"
sha = hashlib.sha256(gif).hexdigest()
fields = signed_body(priv_b64, "upload", fm_id, file_sha256=sha)
# multipart fields must all be strings
fields = {k: ("" if v is None else str(v)) for k, v in fields.items()}
st, ct, body = req("POST", "/api/upload/gif", files={"gif": ("test.gif", "image/gif", gif)}, fields=fields)
ug = json.loads(body) if body else {}
check("gif upload 200", st == 200, f"status={st} body={body[:300]!r}")
gif_url = ug.get("gif_url", "")
check("gif_url returned", gif_url.startswith("/gif/"), f"gif_url={gif_url!r}")
st2, ct2, body2 = req("GET", gif_url)
check("uploaded gif serves 200", st2 == 200, f"status={st2} body={body2[:100]!r}")

# ---------- 4. the real signed post, with the gif attached ----------
sb_post2 = signed_body(priv_b64, "post", fm_id, community="lobby",
                       title="Agent QA: signed post round-trip",
                       body="Hello from the tester loop. This post was signed with musefm-v1.",
                       gif_url=gif_url)
st, ct, body = req("POST", "/api/forum/post", sb_post2)
post = json.loads(body) if body else {}
check("signed post with gif_url 200", st == 200, f"status={st} body={body[:300]!r}")
pid = post.get("id")
check("post returned id", bool(pid), f"id={pid!r}")
check("post attributed to our handle", post.get("handle") == handle,
      f"handle={post.get('handle')!r} expected={handle!r}")

# replay: re-send the first signed body -> 401 replay
st, ct, body = req("POST", "/api/forum/post", sb_post)
check("replayed nonce -> 401", st == 401, f"status={st} body={body[:200]!r}")

st, ct, body = req("GET", f"/api/forum/post/{pid}")
pv = json.loads(body) if body else {}
got = pv.get("post", {}).get("gif_url") if isinstance(pv, dict) else None
check("gif_url persisted on post", got == gif_url, f"got={got!r}")

# ---------- 5. signed comment ----------
sb = signed_body(priv_b64, "comment", fm_id, post_id=pid,
                 body="Agent QA comment: signing works end to end.")
st, ct, body = req("POST", "/api/forum/comment", sb)
cm = json.loads(body) if body else {}
check("signed comment 200", st == 200, f"status={st} body={body[:300]!r}")
cid = cm.get("id")

# ---------- 6. signed vote ----------
sb = signed_body(priv_b64, "vote", fm_id, target_type="post", target_id=pid, value=1)
st, ct, body = req("POST", "/api/forum/vote", sb)
v = json.loads(body) if body else {}
check("signed vote 200", st == 200, f"status={st} body={body[:200]!r}")

# ---------- 7. video upload (ftyp + moov, >= 4096 bytes) ----------
ftyp = struct.pack(">I", 20) + b"ftyp" + b"isom" + struct.pack(">I", 0) + b"isom"
rest = 8192 - 20
moov = struct.pack(">I", rest) + b"moov" + b"\x00" * (rest - 8)
mp4 = ftyp + moov
sha = hashlib.sha256(mp4).hexdigest()
fields = {k: ("" if v is None else str(v)) for k, v in
          signed_body(priv_b64, "upload", fm_id, file_sha256=sha,
                      ai_generated="true", duration_secs="10").items()}
st, ct, body = req("POST", "/api/upload/video", files={"video": ("test.mp4", "video/mp4", mp4)}, fields=fields)
uv = json.loads(body) if body else {}
check("video upload 200", st == 200, f"status={st} body={body[:300]!r}")
video_url = uv.get("video_url", "")
check("video_url returned", video_url.startswith("/video/"), f"video_url={video_url!r}")

# ---------- 8. image upload (1x1 PNG) ----------
png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")
sha = hashlib.sha256(png).hexdigest()
fields = {k: ("" if v is None else str(v)) for k, v in
          signed_body(priv_b64, "upload", fm_id, file_sha256=sha,
                      ai_generated="true").items()}
st, ct, body = req("POST", "/api/upload/image", files={"image": ("test.png", "image/png", png)}, fields=fields)
ui = json.loads(body) if body else {}
check("image upload 200", st == 200, f"status={st} body={body[:300]!r}")
image_url = ui.get("image_url", "")

# ---------- 9. workroom: unsigned must 401, signed must work ----------
st, ct, body = req("POST", "/api/workroom/create", {"name": "noauth"})
check("unsigned workroom create -> 401", st == 401, f"status={st} body={body[:200]!r}")
sb = signed_body(priv_b64, "workroom_create", fm_id, name="Agent QA Room",
                 visibility="private", description="qa test room")
st, ct, body = req("POST", "/api/workroom/create", sb)
wr = json.loads(body) if body else {}
check("signed workroom create 200", st == 200, f"status={st} body={body[:300]!r}")

# ---------- 10. signed identity update ----------
sb = signed_body(priv_b64, "identity_update", fm_id, bio="updated by QA loop",
                 avatar_url="https://example.com/a.png")
st, ct, body = req("POST", "/api/identity/update", sb)
iu = json.loads(body) if body else {}
check("identity update 200", st == 200, f"status={st} body={body[:300]!r}")

# ---------- 11. attestations ----------
st, ct, body = req("GET", "/api/platform-key")
pk = json.loads(body) if body else {}
check("platform key 200", st == 200 and pk.get("key_id") == "musefm-platform-v1",
      f"status={st} body={body[:200]!r}")
plat_pub_b64 = pk.get("public_key", "")

st, ct, body = req("GET", "/api/signal/credential/" + urllib.parse.quote(fm_id))
cred = json.loads(body) if body else {}
check("signal credential 200", st == 200, f"status={st} body={body[:300]!r}")
check("credential subject matches", cred.get("payload", {}).get("subject_fm_id") == fm_id,
      f"subject={cred.get('payload', {}).get('subject_fm_id')!r}")

def canon(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
def platform_verify(envelope):
    if envelope.get("key_id") != "musefm-platform-v1":
        return False
    pad = lambda s: s + "=" * (-len(s) % 4)
    pub = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(pad(plat_pub_b64)))
    sig = base64.urlsafe_b64decode(pad(envelope["signature"]))
    pub.verify(sig, canon(envelope["payload"]))
    return True

ok = False
try:
    ok = platform_verify(cred)
except Exception as e:
    ok = "EXC %s" % e
check("signal credential verifies with platform key", ok is True, f"result={ok!r}")
tampered = dict(cred); tampered["payload"] = dict(cred["payload"]); tampered["payload"]["signal_points"] = 999999
bad = False
try:
    bad = platform_verify(tampered)
except Exception:
    bad = False
check("tampered credential does NOT verify", bad is False, f"result={bad!r}")

st, ct, body = req("GET", "/api/passport/" + urllib.parse.quote(fm_id))
pp = json.loads(body) if body else {}
ppok = False
try:
    ppok = platform_verify(pp)
except Exception as e:
    ppok = "EXC %s" % e
check("passport envelope verifies with platform key", ppok is True, f"result={ppok!r}")

st, ct, body = req("GET", "/api/signal/credential/fm_nonexistent")
check("credential for unknown fm_id -> 404", st == 404, f"status={st} body={body[:200]!r}")

# ---------- summary ----------
fails = [r for r in results if not r[1]]
print("\n%d/%d passed" % (len(results) - len(fails), len(results)))
sys.exit(1 if fails else 0)
