#!/usr/bin/env python3
"""Agent tester follow-up 1835: corrected field names + isolated negative cases."""
import hashlib, json, sys, time, secrets
sys.path.insert(0, "/home/hatch/workspace/musefm-townsquare")
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from identity import b64u_encode, signed_body, sign_fields

BASE = "http://127.0.0.1:8473"
S = requests.Session(); S.trust_env = False
out = []
def log(name, passed, detail=""):
    out.append((name, passed, str(detail)[:400]))
    print(json.dumps({"name": name, "passed": passed, "detail": str(detail)[:250]}))

def kp():
    p = Ed25519PrivateKey.generate()
    return b64u_encode(p.private_bytes_raw()), b64u_encode(p.public_key().public_bytes_raw())

priv_b64, pub_b64 = kp()
h = "ag2_%s" % secrets.token_hex(3)
r = S.post(BASE + "/api/identity/register", json={"handle": h, "public_key": pub_b64})
FM = r.json().get("fm_id", "")
log("register2", r.status_code == 200 and bool(FM), f"{r.status_code}")
def sb(action, **f):
    return signed_body(priv_b64, action, FM, **f)

# uploads with CORRECT multipart field names
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
ub = sb("upload", file_sha256=hashlib.sha256(GIF).hexdigest(), mime="image/gif")
r = S.post(BASE + "/api/upload/gif", data={k: v for k, v in ub.items()},
           files={"gif": ("t.gif", GIF, "image/gif")})
j = r.json() if r.status_code < 500 else {}
GURL = j.get("gif_url", "")
log("gif-upload-fixed-field", r.status_code == 200 and GURL.startswith("/gif/"), f"{r.status_code} {j}")
if GURL:
    log("gif-serve", S.get(BASE + GURL).status_code == 200, "")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
ub = sb("upload", file_sha256=hashlib.sha256(PNG).hexdigest(), mime="image/png", ai_generated=True)
r = S.post(BASE + "/api/upload/image", data={k: v for k, v in ub.items()},
           files={"image": ("t.png", PNG, "image/png")})
j = r.json() if r.status_code < 500 else {}
IURL = j.get("image_url", "")
log("image-upload-fixed-field", r.status_code == 200 and IURL and j.get("ai_generated") is True,
    f"{r.status_code} {j}")
if IURL:
    log("image-serve", S.get(BASE + IURL).status_code == 200, "")

# gif wrong sha / bad magic with correct field
ub = sb("upload", file_sha256="0"*64, mime="image/gif")
r = S.post(BASE + "/api/upload/gif", data={k: v for k, v in ub.items()},
           files={"gif": ("t.gif", GIF, "image/gif")})
log("gif-wrong-sha-400", r.status_code == 400 and "does not match" in r.text, f"{r.status_code} {r.text[:150]}")
ub = sb("upload", file_sha256=hashlib.sha256(b"nope").hexdigest(), mime="image/gif")
r = S.post(BASE + "/api/upload/gif", data={k: v for k, v in ub.items()},
           files={"gif": ("t.bin", b"nope", "image/gif")})
log("gif-bad-magic-400", r.status_code == 400 and "magic" in r.text, f"{r.status_code} {r.text[:150]}")

# audio with correct field
WAV = b"RIFF" + (36).to_bytes(4,"little") + b"WAVEfmt " + (16).to_bytes(4,"little") + b"\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data" + (8000).to_bytes(4,"little") + b"\x00"*8000
ub = sb("upload", file_sha256=hashlib.sha256(WAV).hexdigest(), mime="audio/wav")
r = S.post(BASE + "/api/upload/audio", data={k: v for k, v in ub.items()},
           files={"audio": ("t.wav", WAV, "audio/wav")})
j = r.json() if r.status_code < 500 else {}
log("audio-upload-fixed-field", r.status_code == 200 and ("attestation" in j or "audio_url" in j),
    f"{r.status_code} {str(j)[:200]}")

# memory_write with kind
m = sb("memory_write", kind="note", title="t", body="v")
r = S.post(BASE + "/api/memory", json=m)
log("memory-write-kind-note", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
m = sb("memory_write", kind="bogus", title="t", body="v")
r = S.post(BASE + "/api/memory", json=m)
log("memory-write-bad-kind-400", r.status_code == 400 and "bad kind" in r.text, f"{r.status_code} {r.text[:150]}")

# unsigned body on a fresh endpoint (comment used only 2x this run; budget 30/hr)
r = S.post(BASE + "/api/forum/comment", json={"post_id": 1, "body": "unsigned"})
log("unsigned-body-401", r.status_code == 401, f"{r.status_code} {r.text[:150]}")

# fm-id/key mismatch with a verified second registration
p2b, p2pub = kp()
h2 = "ag2b_%s" % secrets.token_hex(3)
rr = S.post(BASE + "/api/identity/register", json={"handle": h2, "public_key": p2pub})
fm2 = rr.json().get("fm_id", "")
log("register-second", rr.status_code == 200 and bool(fm2), f"{rr.status_code}")
d2 = signed_body(priv_b64, "comment", fm2, post_id=1, body="impersonation attempt")
r = S.post(BASE + "/api/forum/comment", json=d2)
log("fm-id-key-mismatch-401", r.status_code == 401 and "does not verify" in r.text,
    f"{r.status_code} {r.text[:150]}")

# P1 search fix verification on this commit
r = S.get(BASE + "/api/forum/posts", params={"q": "A" * 60000})
log("search-q-60k-400", r.status_code == 400 and "too long" in r.text, f"{r.status_code} {r.text[:150]}")
r = S.get(BASE + "/api/forum/posts", params={"q": "test"})
log("search-q-normal-200", r.status_code == 200, f"{r.status_code}")

# video upload negative: bad magic
ub = sb("upload", file_sha256=hashlib.sha256(b"nope2").hexdigest(), mime="video/mp4", duration_secs="30")
r = S.post(BASE + "/api/upload/video", data={k: v for k, v in ub.items()},
           files={"video": ("t.bin", b"nope2", "video/mp4")})
log("video-bad-magic-400", r.status_code == 400, f"{r.status_code} {r.text[:150]}")
# video duration validation
ub = sb("upload", file_sha256=hashlib.sha256(b"nope3").hexdigest(), mime="video/mp4", duration_secs="99999999")
r = S.post(BASE + "/api/upload/video", data={k: v for k, v in ub.items()},
           files={"video": ("t.bin", b"nope3", "video/mp4")})
log("video-duration-range", r.status_code == 400, f"{r.status_code} {r.text[:150]}")

print("DONE"); fails=[x for x in out if not x[1]]
print("FAILURES:", json.dumps(fails, indent=1)[:3000])
