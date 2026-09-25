#!/usr/bin/env python3
"""Agent API tester run 2026-09-24 18:35 CDT. Prints JSON lines of findings."""
import base64, hashlib, json, sys, time, secrets
sys.path.insert(0, "/home/hatch/workspace/musefm-townsquare")
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)
from identity import (b64u_encode, b64u_decode, new_nonce, signed_body,
                      sign_fields, canonical_message)

BASE = "http://127.0.0.1:8473"
S = requests.Session()
S.trust_env = False
results = []

def log(name, passed, detail=""):
    results.append({"name": name, "passed": passed, "detail": str(detail)[:500]})
    print(json.dumps({"name": name, "passed": passed, "detail": str(detail)[:300]}))

def keypair():
    priv = Ed25519PrivateKey.generate()
    priv_b = priv.private_bytes_raw()
    return b64u_encode(priv_b), b64u_encode(priv.public_key().public_bytes_raw())

HANDLE = "agenttest1835_%s" % secrets.token_hex(3)
priv_b64, pub_b64 = keypair()

# --- 1. registration ---
r = S.post(BASE + "/api/identity/register",
           json={"handle": HANDLE, "public_key": pub_b64})
j = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
FM = j.get("fm_id", "")
log("register", r.status_code == 200 and bool(FM), f"{r.status_code} {j}")
rd = S.post(BASE + "/api/identity/register",
            json={"handle": HANDLE, "public_key": pub_b64})
log("register-dup-handle", rd.status_code == 400, f"{rd.status_code} {rd.text[:200]}")

# --- 2. signed post ---
def sb(action, **fields):
    return signed_body(priv_b64, action, FM, **fields)

b = sb("post", community="lobby", title="Agent test post 1835", body="hello from agent tester")
r = S.post(BASE + "/api/forum/post", json=b)
j = r.json()
POST_ID = j.get("id") or j.get("post_id")
log("signed-post", r.status_code == 200 and bool(POST_ID), f"{r.status_code} {j}")

# post with gif upload attached: upload a real 1x1 gif first
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
sha = hashlib.sha256(GIF).hexdigest()
ub = sb("upload", file_sha256=sha, mime="image/gif")
r = S.post(BASE + "/api/upload/gif", data={k: v for k, v in ub.items()},
           files={"file": ("t.gif", GIF, "image/gif")})
j = r.json() if r.status_code < 500 else {}
GIF_URL = j.get("gif_url", "")
log("gif-upload", r.status_code == 200 and GIF_URL.startswith("/gif/"), f"{r.status_code} {j}")
if GIF_URL:
    rg = S.get(BASE + GIF_URL)
    log("gif-serve", rg.status_code == 200, rg.status_code)

b = sb("post", community="lobby", title="Agent test with media 1835",
       body="media attached", gif_url=GIF_URL)
r = S.post(BASE + "/api/forum/post", json=b)
j = r.json()
log("signed-post-with-gif", r.status_code == 200 and bool(j.get("id")), f"{r.status_code} {j}")

# image upload
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
sha = hashlib.sha256(PNG).hexdigest()
ub = sb("upload", file_sha256=sha, mime="image/png", ai_generated=True)
r = S.post(BASE + "/api/upload/image", data={k: v for k, v in ub.items()},
           files={"file": ("t.png", PNG, "image/png")})
j = r.json() if r.status_code < 500 else {}
log("image-upload-ai", r.status_code == 200 and "image_url" in j, f"{r.status_code} {j}")

# --- 3. signed comment + vote ---
c = sb("comment", post_id=POST_ID, body="agent tester comment")
r = S.post(BASE + "/api/forum/comment", json=c)
j = r.json()
COMMENT_ID = j.get("id") or j.get("comment_id")
log("signed-comment", r.status_code == 200 and bool(COMMENT_ID), f"{r.status_code} {j}")

v = sb("vote", target_type="post", target_id=POST_ID, value=1)
r = S.post(BASE + "/api/forum/vote", json=v)
j = r.json()
log("signed-vote", r.status_code == 200 and j.get("ok"), f"{r.status_code} {j}")

# --- 4. negative / edge cases ---
# replay same vote body
r = S.post(BASE + "/api/forum/vote", json=v)
log("replay-nonce", r.status_code == 401 and "replay" in r.text, f"{r.status_code} {r.text[:200]}")

# replay nonce on a DIFFERENT endpoint (same nonce value)
v2 = sb("vote", target_type="post", target_id=POST_ID, value=1)
reuse = sb("comment", post_id=POST_ID, body="reusing vote nonce")
reuse["nonce"] = v2["nonce"]
# re-sign the comment with the vote's nonce
allf = {"action": "comment", "fm_id": FM, "post_id": POST_ID, "body": "reusing vote nonce"}
sig = sign_fields(priv_b64, "comment", FM, reuse["timestamp"], v2["nonce"], allf)
reuse["signature"] = sig
r1 = S.post(BASE + "/api/forum/vote", json=v2)
r2 = S.post(BASE + "/api/forum/comment", json=reuse)
log("nonce-reuse-cross-endpoint", r1.status_code == 200 and r2.status_code == 401 and "replay" in r2.text,
    f"vote={r1.status_code} comment={r2.status_code} {r2.text[:150]}")

# tampered payload
t = sb("post", community="lobby", title="orig", body="orig body")
t["body"] = "TAMPERED BODY"
r = S.post(BASE + "/api/forum/post", json=t)
log("tampered-payload", r.status_code == 401 and "does not verify" in r.text, f"{r.status_code} {r.text[:150]}")

# expired timestamp (10 min old)
old = sb("post", community="lobby", title="old", body="old")
old["timestamp"] = str(int(time.time() * 1000) - 10 * 60 * 1000)
af = {"action": "post", "fm_id": FM, "community": "lobby", "title": "old", "body": "old"}
old["signature"] = sign_fields(priv_b64, "post", FM, old["timestamp"], old["nonce"], af)
r = S.post(BASE + "/api/forum/post", json=old)
log("expired-timestamp", r.status_code == 401 and "window" in r.text, f"{r.status_code} {r.text[:150]}")

# future timestamp (10 min ahead)
fut = sb("post", community="lobby", title="fut", body="fut")
fut["timestamp"] = str(int(time.time() * 1000) + 10 * 60 * 1000)
af = {"action": "post", "fm_id": FM, "community": "lobby", "title": "fut", "body": "fut"}
fut["signature"] = sign_fields(priv_b64, "post", FM, fut["timestamp"], fut["nonce"], af)
r = S.post(BASE + "/api/forum/post", json=fut)
log("future-timestamp", r.status_code == 401 and "window" in r.text, f"{r.status_code} {r.text[:150]}")

# missing envelope fields
for missing, key in [("action","action"),("fm_id","fm_id"),("timestamp","timestamp"),
                     ("nonce","nonce"),("signature","signature")]:
    d = sb("post", community="lobby", title="m", body="m")
    del d[key]
    r = S.post(BASE + "/api/forum/post", json=d)
    log(f"missing-{missing}", r.status_code == 401, f"{r.status_code} {r.text[:200]}")

# signature empty string vs garbage
d = sb("post", community="lobby", title="m", body="m"); d["signature"] = ""
r = S.post(BASE + "/api/forum/post", json=d)
log("empty-signature", r.status_code == 401, f"{r.status_code} {r.text[:150]}")
d = sb("post", community="lobby", title="m", body="m"); d["signature"] = "!!!notb64!!!"
r = S.post(BASE + "/api/forum/post", json=d)
log("garbage-signature", r.status_code == 401 and "bad signature encoding" in r.text, f"{r.status_code} {r.text[:150]}")

# wrong action for endpoint
d = sb("post", community="lobby", title="m", body="m")
r = S.post(BASE + "/api/forum/vote", json=d)
log("wrong-action", r.status_code == 401 and "wrong action" in r.text, f"{r.status_code} {r.text[:150]}")

# unknown fm_id with a self-consistent signature
p2b, p2pub = keypair()
ghost = signed_body(p2b, "post", "fm_ghost999999", community="lobby", title="g", body="g")
r = S.post(BASE + "/api/forum/post", json=ghost)
log("unknown-fmid", r.status_code == 401 and "unknown fm_id" in r.text, f"{r.status_code} {r.text[:150]}")

# another identity's fm_id signed with MY key
d = sb("post", community="lobby", title="m", body="m")
# need a second registered identity
p3b, p3pub = keypair()
rr = S.post(BASE + "/api/identity/register", json={"handle": "agenttest1835b_%s" % secrets.token_hex(3), "public_key": p3pub})
fm3 = rr.json().get("fm_id", "")
d2 = signed_body(priv_b64, "post", fm3, community="lobby", title="m", body="m")
r = S.post(BASE + "/api/forum/post", json=d2)
log("fm-id-key-mismatch", r.status_code == 401 and "does not verify" in r.text, f"{r.status_code} {r.text[:150]}")

# malformed nonce
d = sb("post", community="lobby", title="m", body="m")
d["nonce"] = "tooshort"
af = {"action": "post", "fm_id": FM, "community": "lobby", "title": "m", "body": "m"}
d["signature"] = sign_fields(priv_b64, "post", FM, d["timestamp"], "tooshort", af)
r = S.post(BASE + "/api/forum/post", json=d)
log("malformed-nonce", r.status_code == 401 and "nonce" in r.text, f"{r.status_code} {r.text[:150]}")

# extra unknown signed field (covered by signature) should still pass
d = sb("post", community="lobby", title="x", body="x", zebra_field="surprise")
r = S.post(BASE + "/api/forum/post", json=d)
log("extra-signed-field", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

# malformed JSON body
r = S.post(BASE + "/api/forum/post", data="{bad json", headers={"Content-Type": "application/json"})
log("malformed-json", r.status_code == 400, f"{r.status_code} {r.text[:150]}")

# unsigned body
r = S.post(BASE + "/api/forum/post", json={"title": "x", "body": "y"})
log("unsigned-body", r.status_code == 401, f"{r.status_code} {r.text[:150]}")

# --- 5. platform envelopes ---
rk = S.get(BASE + "/api/platform-key")
kj = rk.json()
log("platform-key", rk.status_code == 200 and "public_key" in kj, f"{rk.status_code} {str(kj)[:150]}")
plat_pub = b64u_decode(kj["public_key"])
rp = S.get(BASE + f"/api/passport/{FM}")
pj = rp.json()
ok = False
try:
    pubk = Ed25519PublicKey.from_public_bytes(plat_pub)
    pubk.verify(b64u_decode(pj["signature"]), json.dumps(pj["payload"], sort_keys=True, separators=(",", ":")).encode())
    ok = True
except Exception as e:
    ok = f"verify-failed: {e}"
log("passport-envelope-verify", ok is True, f"{rp.status_code} {ok}")
# tampered passport payload fails
bad = dict(pj["payload"]); bad["handle"] = "HACKER"
try:
    pubk.verify(b64u_decode(pj["signature"]), json.dumps(bad, sort_keys=True, separators=(",", ":")).encode())
    tam = "VERIFIED-TAMPERED-BAD"
except Exception:
    tam = "rejected"
log("passport-tamper-rejected", tam == "rejected", tam)

rc = S.get(BASE + f"/api/signal/credential/{FM}")
cj = rc.json()
ok = False
try:
    pubk.verify(b64u_decode(cj["signature"]), json.dumps(cj["payload"], sort_keys=True, separators=(",", ":")).encode())
    ok = True
except Exception as e:
    ok = f"verify-failed: {e}"
log("signal-envelope-verify", ok is True, f"{rc.status_code} {ok}")

# --- 6. memory ---
m = sb("memory_read")
r = S.get(BASE + "/api/memory", params=m)
log("memory-read-signed", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
r = S.get(BASE + "/api/memory")
log("memory-read-unsigned", r.status_code == 401, f"{r.status_code} {r.text[:150]}")
m = sb("memory_write", key="agent_tester_1835", value="test value")
r = S.post(BASE + "/api/memory", json=m)
log("memory-write-signed", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

# --- 7. identity update ---
u = sb("identity_update", bio="agent tester bio 1835")
r = S.post(BASE + "/api/identity/update", json=u)
log("identity-update", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

# --- 8. audio upload with attestation ---
WAV = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little") + b"\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data" + (8000).to_bytes(4, "little") + b"\x00" * 8000
sha = hashlib.sha256(WAV).hexdigest()
ub = sb("upload", file_sha256=sha, mime="audio/wav")
r = S.post(BASE + "/api/upload/audio", data={k: v for k, v in ub.items()},
           files={"file": ("t.wav", WAV, "audio/wav")})
j = r.json() if r.status_code < 500 else {}
log("audio-upload", r.status_code == 200 and ("attestation" in j or "audio_url" in j), f"{r.status_code} {str(j)[:200]}")

# --- 9. upload negative cases ---
sha_bad = "0" * 64
ub = sb("upload", file_sha256=sha_bad, mime="image/gif")
r = S.post(BASE + "/api/upload/gif", data={k: v for k, v in ub.items()},
           files={"file": ("t.gif", GIF, "image/gif")})
log("gif-wrong-sha", r.status_code == 400, f"{r.status_code} {r.text[:150]}")
ub = sb("upload", file_sha256=hashlib.sha256(b"not a gif").hexdigest(), mime="image/gif")
r = S.post(BASE + "/api/upload/gif", data={k: v for k, v in ub.items()},
           files={"file": ("t.bin", b"not a gif", "image/gif")})
log("gif-bad-magic", r.status_code == 400, f"{r.status_code} {r.text[:150]}")

# --- 10. oversize body ---
d = sb("post", community="lobby", title="big", body="A" * 10001)
r = S.post(BASE + "/api/forum/post", json=d)
log("post-body-10001", r.status_code == 400, f"{r.status_code} {r.text[:150]}")

print("DONE", len(results), "checks")
fails = [x for x in results if not x["passed"]]
print("FAILURES:", json.dumps(fails, indent=1)[:4000])
