#!/usr/bin/env python3
"""Agent tester (2026-09-23 12:35 run): negative signed-flow cases for the
musefm-v1 machine API. Complements agent-signed-api-test.py (positives +
attestations). Targets LOCAL scratch instance only. Never touches production."""
import base64, hashlib, json, secrets, struct, sys, time, urllib.request, urllib.parse, urllib.error

BASE = "http://127.0.0.1:8473"
sys.path.insert(0, "/home/hatch/workspace/musefm-townsquare")
from identity import b64u_encode, b64u_decode, signed_body, sign_fields, new_nonce
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), "-", name, (":: " + str(detail) if detail and not cond else ""))

def req(method, path, body=None, raw=None, files=None, fields=None, headers=None):
    url = BASE + path
    data = None
    h = {}
    if files is None:
        if raw is not None:
            data = raw
        elif body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
    else:
        boundary = "----BOUNDARY" + secrets.token_hex(8)
        parts = []
        for k, v in (fields or {}).items():
            parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (k, v)).encode())
        for fname, (fname2, ctype, rawf) in files.items():
            parts.append(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (fname, fname2, ctype)).encode() + rawf + b"\r\n")
        parts.append(("--" + boundary + "--\r\n").encode())
        data = b"".join(parts)
        h["Content-Type"] = "multipart/form-data; boundary=" + boundary
    r = urllib.request.Request(url, data=data, method=method, headers={**h, **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()
    except Exception as e:
        return "EXC", "", ("EXC %s" % e).encode()

def json_body(b):
    try:
        return json.loads(b)
    except Exception:
        return {}

# ---------- keygen + register ----------
priv = Ed25519PrivateKey.generate()
priv_b64 = b64u_encode(priv.private_bytes_raw())
pub_b64 = b64u_encode(priv.public_key().public_bytes_raw())
handle = "negtest" + secrets.token_hex(3)
st, ct, body = req("POST", "/api/identity/register",
                   {"handle": handle, "public_key": pub_b64, "bio": "neg QA"})
reg = json_body(body)
check("register 200", st == 200, f"status={st} body={body[:200]!r}")
fm_id = reg.get("fm_id", "")

def signed(action, **fields):
    return signed_body(priv_b64, action, fm_id, **fields)

POST = {"community": "lobby", "title": "neg QA post", "body": "neg QA body"}

def jst(body):
    return json_body(body)

# ---------- positive control ----------
st, ct, body = req("POST", "/api/forum/post", signed("post", **POST))
p = jst(body)
check("positive signed post 200", st == 200, f"status={st} body={body[:200]!r}")
pid = p.get("id")

# ---------- 1. tampered payload (sign title A, send title B) ----------
b = signed("post", **POST)
b["title"] = "TAMPERED title"
st, ct, body = req("POST", "/api/forum/post", b)
j = jst(body)
check("tampered payload -> 401", st == 401, f"status={st} body={body[:200]!r}")
check("tamper error names signature", "signature" in (j.get("error") or "").lower(), f"error={j.get('error')!r}")

# ---------- 2. wrong nonce formats ----------
for nname, nonce in [("short nonce", "abc"), ("non-base64 nonce", "!!!!not-base64!!!!"), ("empty nonce", "")]:
    b = signed("post", **POST); b["nonce"] = nonce
    st, ct, body = req("POST", "/api/forum/post", b)
    j = jst(body)
    check(f"{nname} -> 401", st == 401, f"status={st} body={body[:200]!r}")
    check(f"{nname} no traceback", b"Traceback" not in body, f"status={st}")

# ---------- 3. replay ----------
b = signed("post", **POST)
st1, _, _ = req("POST", "/api/forum/post", b)
st2, ct, body = req("POST", "/api/forum/post", b)
check("replay -> 401", st1 == 200 and st2 == 401, f"first={st1} second={st2} body={body[:200]!r}")
j = jst(body)
check("replay error says replay", "replay" in (j.get("error") or "").lower(), f"error={j.get('error')!r}")

# ---------- 4. garbage signature ----------
b = signed("post", **POST); b["signature"] = "not-a-signature!!!"
st, ct, body = req("POST", "/api/forum/post", b)
check("garbage signature -> 401", st == 401, f"status={st} body={body[:200]!r}")
check("garbage signature no 500/traceback", st != 500 and b"Traceback" not in body, f"status={st}")
b = signed("post", **POST); b["signature"] = b64u_encode(b"\x00" * 64)  # well-formed, wrong key material
st, ct, body = req("POST", "/api/forum/post", b)
check("zero-bytes signature -> 401", st == 401, f"status={st} body={body[:200]!r}")
b = signed("post", **POST); b["signature"] = b64u_encode(b"\x00" * 64) + "extra"
st, ct, body = req("POST", "/api/forum/post", b)
check("65-byte signature -> 401", st == 401, f"status={st} body={body[:200]!r}")

# ---------- 5. missing signature fields ----------
for fname, drop in [("missing signature", "signature"), ("missing action", "action"),
                    ("missing fm_id", "fm_id"), ("missing timestamp", "timestamp"),
                    ("missing nonce", "nonce")]:
    b = signed("post", **POST); del b[drop]
    st, ct, body = req("POST", "/api/forum/post", b)
    j = jst(body)
    ok_status = st in (400, 401)
    check(f"{fname} -> 4xx", ok_status, f"status={st} body={body[:200]!r}")
    check(f"{fname} no 500/traceback", st != 500 and b"Traceback" not in body, f"status={st}")

# ---------- 6. expired / future timestamps ----------
def signed_with_ts(ts_ms, action, **fields):
    nonce = new_nonce()
    all_fields = {"action": action, **fields}
    sig = sign_fields(priv_b64, action, fm_id, str(ts_ms), nonce, all_fields)
    return {"action": action, "fm_id": fm_id, "timestamp": str(ts_ms),
            "nonce": nonce, "signature": sig, **fields}

old = signed_with_ts(int(time.time() * 1000) - 10 * 60 * 1000, "post", **POST)
st, ct, body = req("POST", "/api/forum/post", old)
j = jst(body)
check("expired timestamp -> 401", st == 401, f"status={st} body={body[:200]!r}")
check("expired error names window", "window" in (j.get("error") or "").lower(), f"error={j.get('error')!r}")
fut = signed_with_ts(int(time.time() * 1000) + 10 * 60 * 1000, "post", **POST)
st, ct, body = req("POST", "/api/forum/post", fut)
check("future timestamp -> 401", st == 401, f"status={st} body={body[:200]!r}")
bad = signed_with_ts("not-a-time", "post", **POST)
st, ct, body = req("POST", "/api/forum/post", bad)
check("garbage timestamp -> 401", st == 401, f"status={st} body={body[:200]!r}")

# ---------- 7. wrong action for endpoint ----------
b = signed("comment", post_id=pid, body="x")
st, ct, body = req("POST", "/api/forum/post", b)
j = jst(body)
check("wrong action for endpoint -> 401", st == 401, f"status={st} body={body[:200]!r}")
check("wrong action names action", "action" in (j.get("error") or "").lower(), f"error={j.get('error')!r}")

# ---------- 8. unknown fm_id ----------
b = signed("post", **POST); b["fm_id"] = "fm_" + b64u_encode(secrets.token_bytes(9))
st, ct, body = req("POST", "/api/forum/post", b)
check("unknown fm_id -> 401", st == 401, f"status={st} body={body[:200]!r}")

# ---------- 9. malformed envelopes -> clean 4xx, never 500 ----------
st, ct, body = req("POST", "/api/forum/post", raw=b"{not json")
check("malformed JSON -> 400", st == 400, f"status={st} body={body[:200]!r}")
check("malformed JSON no traceback", b"Traceback" not in body, f"status={st}")
st, ct, body = req("POST", "/api/forum/post", raw=b"[1,2,3]")
check("JSON array body -> 400", st == 400, f"status={st} body={body[:200]!r}")
st, ct, body = req("POST", "/api/forum/post", body=None)
check("empty body -> 4xx", st in (400, 401), f"status={st} body={body[:200]!r}")
check("empty body no 500", st != 500 and b"Traceback" not in body, f"status={st}")
st, ct, body = req("POST", "/api/forum/post", raw=b'{"action": 5, "fm_id": 1, "timestamp": {}, "nonce": [], "signature": true}')
j = jst(body)
check("wrong-typed fields -> 4xx", st in (400, 401), f"status={st} body={body[:200]!r}")
check("wrong-typed fields no 500", st != 500 and b"Traceback" not in body, f"status={st}")

# ---------- 10. unsigned must 401 on every signed endpoint ----------
for pname, path, act, extra in [
    ("comment", "/api/forum/comment", "comment", {"post_id": pid, "body": "x"}),
    ("vote", "/api/forum/vote", "vote", {"target_type": "post", "target_id": pid, "value": 1}),
    ("identity_update", "/api/identity/update", "identity_update", {"bio": "x"}),
    ("workroom_create", "/api/workroom/create", "workroom_create", {"name": "x"}),
]:
    st, ct, body = req("POST", path, extra)
    check(f"unsigned {pname} -> 401", st == 401, f"status={st} body={body[:200]!r}")

# ---------- 11. signed comment + vote + double-vote ----------
sb = signed("comment", post_id=pid, body="agent QA comment")
st, ct, body = req("POST", "/api/forum/comment", sb)
c = jst(body)
check("signed comment 200", st == 200, f"status={st} body={body[:200]!r}")
sb = signed("vote", target_type="post", target_id=pid, value=1)
st, ct, body = req("POST", "/api/forum/vote", sb)
check("signed vote 200", st == 200, f"status={st} body={body[:200]!r}")
sb = signed("vote", target_type="post", target_id=pid, value=1)
st, ct, body = req("POST", "/api/forum/vote", sb)
j = jst(body)
check("double-vote idempotent/no-crash (200 or 4xx, not 500)",
      st in (200, 400, 401, 409), f"status={st} body={body[:200]!r}")
sb = signed("vote", target_type="post", target_id=pid, value=7)
st, ct, body = req("POST", "/api/forum/vote", sb)
check("bad vote value -> 4xx not 500", st in (200, 400, 401, 409), f"status={st} body={body[:200]!r}")
check("bad vote value no traceback", b"Traceback" not in body, f"status={st}")

# ---------- 12. media: gif upload + attach, sha mismatch, bad magic ----------
gif = (b"GIF89a" + b"\x01\x00\x01\x00\x80\x00\x00" + b"\xff\xff\xff"
       + b"\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b")
sha = hashlib.sha256(gif).hexdigest()
fields = {k: ("" if v is None else str(v)) for k, v in
          signed_body(priv_b64, "upload", fm_id, file_sha256=sha).items()}
st, ct, body = req("POST", "/api/upload/gif", files={"gif": ("t.gif", "image/gif", gif)}, fields=fields)
ug = jst(body)
check("signed gif upload 200", st == 200, f"status={st} body={body[:300]!r}")
gif_url = ug.get("gif_url", "")
check("gif_url shape", gif_url.startswith("/gif/"), f"gif_url={gif_url!r}")

# sha mismatch
fields = {k: ("" if v is None else str(v)) for k, v in
          signed_body(priv_b64, "upload", fm_id, file_sha256="0"*64).items()}
st, ct, body = req("POST", "/api/upload/gif", files={"gif": ("t.gif", "image/gif", gif)}, fields=fields)
check("gif sha mismatch -> 4xx", st in (400, 401), f"status={st} body={body[:200]!r}")
check("gif sha mismatch no 500", st != 500 and b"Traceback" not in body, f"status={st}")

# fake gif (PNG bytes renamed .gif)
png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")
fields = {k: ("" if v is None else str(v)) for k, v in
          signed_body(priv_b64, "upload", fm_id, file_sha256=hashlib.sha256(png).hexdigest()).items()}
st, ct, body = req("POST", "/api/upload/gif", files={"gif": ("t.gif", "image/gif", png)}, fields=fields)
check("fake-gif (PNG magic) -> 4xx", st in (400, 401), f"status={st} body={body[:200]!r}")

# post with the gif attached
sb = signed("post", community="lobby", title="neg QA gif post",
            body="post with attached gif", gif_url=gif_url)
st, ct, body = req("POST", "/api/forum/post", sb)
gp = jst(body)
check("signed post with gif_url 200", st == 200, f"status={st} body={body[:200]!r}")
gpid = gp.get("id")
st2, _, body2 = req("GET", f"/api/forum/post/{gpid}")
pv = jst(body2)
got = pv.get("post", {}).get("gif_url")
check("gif_url persisted", got == gif_url, f"got={got!r}")

# post with external gif URL (not allowlisted host) -> must be rejected
sb = signed("post", community="lobby", title="evil gif post", body="x",
            gif_url="https://evil.example/x.gif")
st, ct, body = req("POST", "/api/forum/post", sb)
check("non-allowlisted gif host -> 4xx", st in (400, 401), f"status={st} body={body[:200]!r}")

# ---------- 13. image upload + video upload (multipart bool str "True") ----------
fields = {k: ("" if v is None else str(v)) for k, v in
          signed_body(priv_b64, "upload", fm_id, file_sha256=hashlib.sha256(png).hexdigest(),
                      ai_generated="true").items()}
st, ct, body = req("POST", "/api/upload/image", files={"image": ("t.png", "image/png", png)}, fields=fields)
ui = jst(body)
check("signed image upload 200", st == 200, f"status={st} body={body[:300]!r}")

ftyp = struct.pack(">I", 20) + b"ftyp" + b"isom" + struct.pack(">I", 0) + b"isom"
rest = 8192 - 20
mp4 = ftyp + struct.pack(">I", rest) + b"moov" + b"\x00" * (rest - 8)
fields = {k: ("" if v is None else str(v)) for k, v in
          signed_body(priv_b64, "upload", fm_id,
                      file_sha256=hashlib.sha256(mp4).hexdigest(),
                      ai_generated="True", duration_secs="10").items()}
st, ct, body = req("POST", "/api/upload/video", files={"video": ("t.mp4", "video/mp4", mp4)}, fields=fields)
uv = jst(body)
check("signed video upload (multipart bool 'True') 200", st == 200, f"status={st} body={body[:300]!r}")

# ---------- 14. attestations ----------
st, ct, body = req("GET", "/api/platform-key")
pk = jst(body)
check("platform key 200", st == 200 and pk.get("key_id") == "musefm-platform-v1",
      f"status={st} body={body[:200]!r}")
plat_pub = pk.get("public_key", "")
def canon(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
def platform_verify(env):
    pad = lambda s: s + "=" * (-len(s) % 4)
    pub = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(pad(plat_pub)))
    sig = base64.urlsafe_b64decode(pad(env["signature"]))
    pub.verify(sig, canon(env["payload"]))
    return True
st, ct, body = req("GET", "/api/signal/credential/" + urllib.parse.quote(fm_id))
cred = jst(body)
check("signal credential 200", st == 200, f"status={st} body={body[:200]!r}")
try:
    ok = platform_verify(cred)
except Exception as e:
    ok = "EXC %s" % e
check("credential verifies with platform key", ok is True, f"result={ok!r}")
st, ct, body = req("GET", "/api/passport/" + urllib.parse.quote(fm_id))
pp = jst(body)
try:
    pok = platform_verify(pp)
except Exception as e:
    pok = "EXC %s" % e
check("passport verifies with platform key", pok is True, f"result={pok!r}")

# ---------- summary ----------
fails = [r for r in results if not r[1]]
print("\n%d/%d passed" % (len(results) - len(fails), len(results)))
sys.exit(1 if fails else 0)
