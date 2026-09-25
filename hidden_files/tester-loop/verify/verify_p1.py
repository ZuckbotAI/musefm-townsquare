#!/usr/bin/env python3
"""Verify the two agent-tester P1 candidates on a fresh bucket (127.0.0.2). Local only."""
import base64, hashlib, json, os, secrets, sys, urllib.request, urllib.error
sys.path.insert(0, "/home/hatch/workspace/musefm-townsquare")
from identity import b64u_encode, signed_body
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE = "http://127.0.0.2:8473"
HERE = os.path.dirname(os.path.abspath(__file__))

def req(method, path, body=None, raw=None, ctype="application/json"):
    data, headers = None, {}
    if body is not None:
        data = json.dumps(body).encode(); headers["Content-Type"] = ctype
    if raw is not None:
        data = raw; headers["Content-Type"] = ctype
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=60)
        return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")

def multipart(path, fields, file_field, filename, raw, mime):
    boundary = "----v" + secrets.token_hex(8)
    parts = []
    for k, v in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (boundary, file_field, filename, mime)).encode() + raw + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    return req("POST", path, raw=b"".join(parts), ctype="multipart/form-data; boundary=" + boundary)

def jload(s):
    try: return json.loads(s)
    except Exception: return None

priv = Ed25519PrivateKey.generate()
PRIV_B64 = b64u_encode(priv.private_bytes_raw())
PUB_B64 = b64u_encode(priv.public_key().public_bytes_raw())
HANDLE = "verify_" + secrets.token_hex(3)
st, body = req("POST", "/api/identity/register", body={"handle": HANDLE, "public_key": PUB_B64})
d = jload(body) or {}; FM_ID = d.get("fm_id", "")
print("register:", st, "fm_id:", FM_ID)
assert st == 200 and FM_ID.startswith("fm_"), "registration failed"

mp4_raw = open(os.path.join(HERE, "test.mp4"), "rb").read()
print("full mp4 bytes:", len(mp4_raw))

# --- Candidate 1: truncated video (ftyp only, no moov) should be rejected
trunc = mp4_raw[:4200]
flds = signed_body(PRIV_B64, "upload", FM_ID, file_sha256=hashlib.sha256(trunc).hexdigest(), ai_generated="true")
flds = {k: str(v) for k, v in flds.items()}
st, body = multipart("/api/upload/video", flds, "video", "trunc.mp4", trunc, "video/mp4")
d = jload(body) or {}
print("CANDIDATE-1 truncated video upload ->", st, json.dumps(d)[:220])

# --- Candidate 2: comment with video_url should persist the attachment
flds = signed_body(PRIV_B64, "upload", FM_ID, file_sha256=hashlib.sha256(mp4_raw).hexdigest(), ai_generated="true")
flds = {k: str(v) for k, v in flds.items()}
st, body = multipart("/api/upload/video", flds, "video", "test.mp4", mp4_raw, "video/mp4")
d = jload(body) or {}
VID = d.get("id")
print("video upload ->", st, "id:", VID, "url:", d.get("video_url"))

b = signed_body(PRIV_B64, "post", FM_ID, community="lobby", title="verify post", body="verify body")
st, body = req("POST", "/api/forum/post", body=b)
d = jload(body) or {}
PID = d.get("id")
print("signed post ->", st, "pid:", PID, (jload(body) or {}).get("error"))

b = signed_body(PRIV_B64, "comment", FM_ID, post_id=PID, body="comment with video attach", video_url=f"/video/{VID}", video_ai=True)
st, body = req("POST", "/api/forum/comment", body=b)
d = jload(body) or {}
CID = d.get("id")
print("comment with video_url ->", st, "cid:", CID, body[:160])

st, body = req("GET", f"/api/forum/post/{PID}")
d = jload(body) or {}
found = None
for c in (d.get("comments") or []):
    if c.get("id") == CID:
        found = c
print("CANDIDATE-2 comment video_url persisted:", (found or {}).get("video_url"), "| comment keys:", sorted((found or {}).keys()))
