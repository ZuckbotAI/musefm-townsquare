#!/usr/bin/env python3
"""AGENT TESTER — Muse FM machine-API QA against local test instance.
Commit under test: 86e3684. Server: http://127.0.0.1:8473 (scratch DB).
Reads identity.py for the musefm-v1 scheme; exercises signed flows."""
import base64, hashlib, json, sys, time, urllib.request, urllib.error
import urllib.parse

sys.path.insert(0, "/home/hatch/workspace/musefm-townsquare")
import identity as ID
from identity import IdentityError

BASE = "http://127.0.0.1:8473"
RESULTS = []  # (name, ok, detail)

def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

# --- raw HTTP with NO proxy (localhost) --------------------------------
def req(method, path, body=None, ctype="application/json", raw_bytes=None):
    url = BASE + path
    data = None
    headers = {}
    if raw_bytes is not None:
        data = raw_bytes
    elif body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = body
    if data is not None and ctype:
        headers["Content-Type"] = ctype
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"TRANSPORT ERROR: {type(e).__name__}: {e}"

def jparse(body):
    try: return json.loads(body)
    except Exception: return None

def report(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("PASS" if ok else "FAIL"), "-", name, ("| " + detail[:220] if detail else ""))

# --- keys & signing -------------------------------------------------------
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

def gen_key():
    priv = Ed25519PrivateKey.generate()
    pb = priv.public_key().public_bytes(
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).Encoding.Raw,
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).PrivateFormat.Raw,
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).NoEncryption())
    return priv, pb

# simpler: use cryptography directly
from cryptography.hazmat.primitives import serialization
def gen_keypair():
    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption())
    return b64u(priv_raw), b64u(pub_raw)

PRIV_B64, PUB_B64 = gen_keypair()
PRIV2_B64, PUB2_B64 = gen_keypair()  # wrong key for tamper tests
HANDLE = "qabot_%d" % int(time.time() % 1000000)

def signed(action, fm_id, priv_b64, fields=None, ts_ms=None, nonce=None,
           skip_fields=()):
    fields = dict(fields or {})
    ts = str(int(time.time() * 1000)) if ts_ms is None else ts_ms
    nc = ID.new_nonce() if nonce is None else nonce
    allf = {"action": action, **fields}
    sig = ID.sign_fields(priv_b64, action, fm_id, ts, nc, allf)
    body = {"action": action, "fm_id": fm_id, "timestamp": ts,
            "nonce": nc, "signature": sig, **fields}
    for k in skip_fields:
        body.pop(k, None)
    return body

def multipart(fields, file_field, filename, raw, fctype):
    boundary = "----QA%x" % int(time.time()*1000)
    parts = []
    for k, v in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, k, v)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                  "Content-Type: %s\r\n\r\n" % (boundary, file_field, filename, fctype)).encode()
                 + raw + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary

# ============================================================ POSITIVE FLOWS
print("=== 1. register identity ===")
st, b = req("POST", "/api/identity/register",
            {"handle": HANDLE, "public_key": PUB_B64, "bio": "qa agent bot"})
d = jparse(b)
FM = (d.get("fm_id") if d else None)
report("register identity (unsigned, plaintext fields)", st == 200 and d and d.get("ok") and FM,
       f"status={st} body={b[:160]}")
if not FM:
    print("FATAL: no fm_id"); sys.exit(1)

print("=== 2. profile ===")
st, b = req("GET", "/api/identity/" + FM)
d = jparse(b)
report("GET /api/identity/<fm_id>", st == 200 and d and d.get("identity", {}).get("handle") == HANDLE,
       f"status={st} body={b[:160]}")

print("=== 3. signed post ===")
post_fields = {"community": "lobby", "title": "QA agent post 🤖",
               "body": "Hello from the agent tester. Unicode: 你好世界 émojis 🎉",
               "flair": "discussion"}
st, b = req("POST", "/api/forum/post", signed("post", FM, PRIV_B64, post_fields))
d = jparse(b)
PID = d.get("id") if d else None
report("signed post (action=post, unicode body)", st == 200 and d and d.get("ok") and PID,
       f"status={st} body={b[:200]}")

print("=== 4. signed comment ===")
st, b = req("POST", "/api/forum/comment",
            signed("comment", FM, PRIV_B64, {"post_id": PID, "body": "agent comment #1"}))
d = jparse(b)
CID = d.get("id") if d else None
report("signed comment (action=comment)", st == 200 and d and d.get("ok") and CID,
       f"status={st} body={b[:160]}")

print("=== 5. signed vote ===")
st, b = req("POST", "/api/forum/vote",
            signed("vote", FM, PRIV_B64, {"target_type": "post", "target_id": PID, "value": 1}))
d = jparse(b)
report("signed vote (action=vote)", st == 200 and d and d.get("ok"),
       f"status={st} body={b[:160]}")

print("=== 6. identity_update ===")
st, b = req("POST", "/api/identity/update",
            signed("identity_update", FM, PRIV_B64, {"bio": "updated by qa agent"}))
d = jparse(b)
report("signed identity_update (bio)", st == 200 and d and d.get("ok") and
       d.get("identity", {}).get("bio") == "updated by qa agent",
       f"status={st} body={b[:160]}")

print("=== 7. signed GIF upload (multipart attestation) ===")
# minimal real GIF: 1x1 GIF89a
gif = (b"GIF89a" + bytes([1,0,1,0,0x80,0,0,0,0,0,0,0,0,0x21,0xf9,0x04,0x01,0,0,0,0,
        0x2c,0,0,0,0,1,0,1,0,0,2,2,0x44,0x01,0,0x3b]))
sha = hashlib.sha256(gif).hexdigest()
uf = {"action": "upload", "file_sha256": sha}
sbody = signed("upload", FM, PRIV_B64, uf)
mp, ct = multipart({k: str(v) for k, v in sbody.items()}, "gif", "qa.gif", gif, "image/gif")
st, b = req("POST", "/api/upload/gif", raw_bytes=mp, ctype=ct)
d = jparse(b)
GIFURL = d.get("gif_url") if d else None
report("signed GIF upload (magic bytes, sha256 attestation)",
       st == 200 and d and d.get("ok") and GIFURL,
       f"status={st} body={b[:200]}")

print("=== 8. post with gif_url attachment ===")
st, b = req("POST", "/api/forum/post",
            signed("post", FM, PRIV_B64,
                   {"community": "lobby", "title": "post with gif",
                    "body": "gif attached", "gif_url": GIFURL or "/gif/1"}))
d = jparse(b)
PID2 = d.get("id") if d else None
report("signed post w/ gif_url", st == 200 and d and d.get("ok"),
       f"status={st} body={b[:160]}")

print("=== 9. attestations verify offline ===")
st, b = req("GET", "/api/platform-key")
pk = jparse(b)
key_ok = st == 200 and pk and pk.get("key_id") == "musefm-platform-v1" and pk.get("public_key")
report("GET /api/platform-key", bool(key_ok), f"status={st} body={b[:120]}")

import trustline_bridge as tb
def verify_envelope(env):
    pub = Ed25519PublicKey.from_public_bytes(
        base64.urlsafe_b64decode(pk["public_key"] + "=" * (-len(pk["public_key"]) % 4)))
    sig = base64.urlsafe_b64decode(env["signature"] + "=" * (-len(env["signature"]) % 4))
    pub.verify(sig, tb.canonical(env["payload"]))
    return True

st, b = req("GET", "/api/signal/credential/" + FM)
d = jparse(b)
try:
    att_ok = st == 200 and d and d.get("key_id") == "musefm-platform-v1" and verify_envelope(d)
    det = "sig verifies offline" if att_ok else f"status={st} body={b[:120]}"
except Exception as e:
    att_ok, det = False, f"verify raised {e}"
report("platform attestation: /api/signal/credential verifies offline", att_ok, det)

st, b = req("GET", "/api/passport/" + FM)
d = jparse(b)
try:
    p_ok = st == 200 and d and verify_envelope(d)
    det = "sig verifies offline" if p_ok else f"status={st} body={b[:120]}"
except Exception as e:
    p_ok, det = False, f"verify raised {e}"
report("platform attestation: /api/passport verifies offline", p_ok, det)

st, b = req("GET", "/api/agents/%s/activity?signed=1" % FM)
d = jparse(b)
try:
    a_ok = st == 200 and d and verify_envelope(d)
    det = "sig verifies offline" if a_ok else f"status={st} body={b[:160]}"
except Exception as e:
    a_ok, det = False, f"verify raised {e}"
report("platform attestation: /api/agents/<id>/activity?signed=1 verifies", a_ok, det)

print("=== 10. signed GET notifications ===")
def signed_query(action):
    ts = str(int(time.time()*1000)); nc = ID.new_nonce()
    sig = ID.sign_fields(PRIV_B64, action, FM, ts, nc, {"action": action})
    return urllib.parse.urlencode({"action": action, "fm_id": FM, "timestamp": ts,
                                   "nonce": nc, "signature": sig})
st, b = req("GET", "/api/notifications?" + signed_query("notifications"))
d = jparse(b)
report("signed GET /api/notifications (query-param signature)",
       st == 200 and d and d.get("ok"), f"status={st} body={b[:120]}")

# ============================================================ NEGATIVE TESTS
print("=== NEGATIVE ===")
def neg(name, expect_status, expect_substr, method, path, body=None, ctype="application/json", raw=None):
    st, b = req(method, path, body=body, ctype=ctype, raw_bytes=raw)
    ok = st == expect_status and (expect_substr in b if expect_substr else True)
    report(name, ok, f"expected {expect_status}~{expect_substr!r}; got {st} body={b[:200]}")

# N1 tampered payload: sign then alter body field
tb_body = signed("post", FM, PRIV_B64, {"community": "lobby", "title": "t1", "body": "original"})
tb_body["body"] = "TAMPERED"
neg("tampered payload rejected", 401, "signature does not verify", "POST", "/api/forum/post", tb_body)

# N2 wrong key
neg("signature from wrong key rejected", 401, "signature does not verify", "POST", "/api/forum/post",
    signed("post", FM, PRIV2_B64, {"community": "lobby", "title": "t2", "body": "x"}))

# N3 replay: identical body twice
rb = signed("vote", FM, PRIV_B64, {"target_type": "post", "target_id": PID, "value": 1})
st1, b1 = req("POST", "/api/forum/vote", rb)
st2, b2 = req("POST", "/api/forum/vote", rb)
report("replayed signature rejected (nonce burned)", st1 == 200 and st2 == 401 and "replay" in b2,
       f"first={st1} second={st2} body2={b2[:160]}")

# N4 replay across endpoints with re-signed same nonce
nc = ID.new_nonce()
bA = signed("vote", FM, PRIV_B64, {"target_type": "post", "target_id": PID, "value": 1}, nonce=nc)
bB = signed("vote", FM, PRIV_B64, {"target_type": "post", "target_id": PID, "value": -1}, nonce=nc)
st1, b1 = req("POST", "/api/forum/vote", bA)
st2, b2 = req("POST", "/api/forum/vote", bB)
report("same nonce reused on re-signed body rejected", st1 == 200 and st2 == 401 and "replay" in b2,
       f"first={st1} second={st2} body2={b2[:160]}")

# N5 expired timestamp (10 min old)
old = str(int(time.time()*1000) - 10*60*1000)
neg("expired timestamp (10min old) rejected", 401, "outside the 5-minute window", "POST", "/api/forum/post",
    signed("post", FM, PRIV_B64, {"community": "lobby", "title": "t5", "body": "x"}, ts_ms=old))

# N6 future timestamp (10 min)
fut = str(int(time.time()*1000) + 10*60*1000)
neg("future timestamp (10min) rejected", 401, "outside the 5-minute window", "POST", "/api/forum/post",
    signed("post", FM, PRIV_B64, {"community": "lobby", "title": "t6", "body": "x"}, ts_ms=fut))

# N7 edge of window: 4m59s should pass, 5m01s fail
edge_ok = str(int(time.time()*1000) - (4*60+59)*1000)
edge_bad = str(int(time.time()*1000) - (5*60+1)*1000)
st, b = req("POST", "/api/forum/vote",
            signed("vote", FM, PRIV_B64, {"target_type": "post", "target_id": PID, "value": 1}, ts_ms=edge_ok))
d = jparse(b)
report("timestamp 4m59s old accepted", st == 200 and d and d.get("ok"), f"status={st} body={b[:120]}")
st, b = req("POST", "/api/forum/vote",
            signed("vote", FM, PRIV_B64, {"target_type": "post", "target_id": PID, "value": 1}, ts_ms=edge_bad))
report("timestamp 5m01s old rejected", st == 401 and "outside the 5-minute window" in b,
       f"status={st} body={b[:120]}")

# N8 missing fields
base = signed("post", FM, PRIV_B64, {"community": "lobby", "title": "t8", "body": "x"})
for f, msg in [("signature", "auth failed"), ("action", "missing action"),
               ("fm_id", "missing fm_id"), ("timestamp", "missing timestamp"),
               ("nonce", "missing nonce")]:
    c = dict(base); c.pop(f)
    neg(f"missing {f} rejected", 401, msg, "POST", "/api/forum/post", c)

# N9 wrong action for endpoint
neg("wrong action for endpoint rejected", 401, "wrong action for this endpoint",
    "POST", "/api/forum/post",
    signed("vote", FM, PRIV_B64, {"target_type": "post", "target_id": PID, "value": 1}))

# N10 unknown fm_id
neg("unknown fm_id rejected", 401, "unknown fm_id", "POST", "/api/forum/post",
    signed("post", "fm_AAAAAAAAAAAAAAAA", PRIV_B64, {"community": "lobby", "title": "t", "body": "x"}))

# N11 bad nonce (short)
neg("bad nonce rejected", 401, "bad nonce", "POST", "/api/forum/post",
    signed("post", FM, PRIV_B64, {"community": "lobby", "title": "t", "body": "x"}, nonce="abcd"))

# N12 malformed signature encoding
c = signed("post", FM, PRIV_B64, {"community": "lobby", "title": "t", "body": "x"})
c["signature"] = "!!!not-base64!!!"
neg("malformed signature rejected", 401, "bad signature encoding", "POST", "/api/forum/post", c)

# N13 bad timestamp format
c = signed("post", FM, PRIV_B64, {"community": "lobby", "title": "t", "body": "x"})
c["timestamp"] = "yesterday"
neg("non-numeric timestamp rejected", 401, "bad timestamp", "POST", "/api/forum/post", c)

# N14 wrong content-type / garbage body
st, b = req("POST", "/api/forum/post", body="this is not json {{{",
            ctype="text/plain")
report("garbage non-JSON body -> 400 (not 401/500)", st == 400 and "Malformed JSON" in b,
       f"status={st} body={b[:160]}")
st, b = req("POST", "/api/forum/post", raw_bytes=b"a=1&b=2",
            ctype="application/x-www-form-urlencoded")
report("form-encoded garbage -> 400 (not 401)", st == 400 and "Malformed JSON" in b,
       f"status={st} body={b[:160]}")

# N15 empty body
st, b = req("POST", "/api/forum/post", raw_bytes=b"")
report("empty body on signed endpoint -> 401 missing action", st == 401 and "missing action" in b,
       f"status={st} body={b[:160]}")

# N16 JSON array body
st, b = req("POST", "/api/forum/post", body=[1, 2, 3])
report("JSON array body -> 400 must be an object", st == 400 and "must be an object" in b,
       f"status={st} body={b[:160]}")

# N17 sha256 mismatch on GIF upload
uf = signed("upload", FM, PRIV_B64, {"action": "upload", "file_sha256": "0"*64})
mp, ct = multipart({k: str(v) for k, v in uf.items()}, "gif", "qa.gif", gif, "image/gif")
st, b = req("POST", "/api/upload/gif", raw_bytes=mp, ctype=ct)
report("GIF upload w/ wrong file_sha256 -> 401", st == 401 and "does not match" in b,
       f"status={st} body={b[:160]}")

# N18 non-GIF bytes as GIF
png = b"\x89PNG\r\n\x1a\n" + b"\x00"*100
uf = signed("upload", FM, PRIV_B64, {"file_sha256": hashlib.sha256(png).hexdigest()})
mp, ct = multipart({k: str(v) for k, v in uf.items()}, "gif", "qa.png", png, "image/png")
st, b = req("POST", "/api/upload/gif", raw_bytes=mp, ctype=ct)
report("GIF upload w/ PNG bytes -> 400 not a gif", st == 400 and "not a gif" in b,
       f"status={st} body={b[:160]}")

# N19 tampered signed multipart field (file_sha256 altered after signing)
uf = signed("upload", FM, PRIV_B64, {"file_sha256": sha})
uf["file_sha256"] = sha[:-2] + ("00" if not sha.endswith("00") else "ff")
mp, ct = multipart({k: str(v) for k, v in uf.items()}, "gif", "qa.gif", gif, "image/gif")
st, b = req("POST", "/api/upload/gif", raw_bytes=mp, ctype=ct)
report("GIF upload w/ tampered signed file_sha256 -> 401", st == 401 and "signature does not verify" in b,
       f"status={st} body={b[:160]}")

# N20 multipart bool True rendering (P2 2026-09-19 fix)
uf = signed("upload", FM, PRIV_B64, {"file_sha256": sha, "extra_flag": True})
mp, ct = multipart({k: str(v) for k, v in uf.items()}, "gif", "qa.gif", gif, "image/gif")
st, b = req("POST", "/api/upload/gif", raw_bytes=mp, ctype=ct)
d = jparse(b)
report("multipart bool 'True' survives signing (P2 fix holds)", st == 200 and d and d.get("ok"),
       f"status={st} body={b[:160]}")

# N21 registration validation
neg("register duplicate handle -> 400", 400, "taken", "POST", "/api/identity/register",
    {"handle": HANDLE, "public_key": PUB_B64})
neg("register bad handle -> 400", 400, "bad handle", "POST", "/api/identity/register",
    {"handle": "x", "public_key": PUB_B64})
neg("register bad public_key -> 400", 400, "bad public_key", "POST", "/api/identity/register",
    {"handle": "qabot_new_%d" % int(time.time()%100000), "public_key": "zzzz"})
neg("register reserved handle -> 400", 400, "reserved", "POST", "/api/identity/register",
    {"handle": "admin", "public_key": PUB2_B64})

# N22 comment validation
neg("comment on unknown post -> 400", 400, "unknown post", "POST", "/api/forum/comment",
    signed("comment", FM, PRIV_B64, {"post_id": 99999999, "body": "hello"}))
neg("comment empty body -> 400", 400, "comment body required", "POST", "/api/forum/comment",
    signed("comment", FM, PRIV_B64, {"post_id": PID, "body": "   "}))

# N23 post validation
neg("post unknown community -> 400", 400, "unknown community", "POST", "/api/forum/post",
    signed("post", FM, PRIV_B64, {"community": "nope", "title": "t", "body": "x"}))
neg("post missing title -> 400", 400, "title required", "POST", "/api/forum/post",
    signed("post", FM, PRIV_B64, {"community": "lobby", "body": "x"}))
neg("post 500-char title -> 400", 400, "too long", "POST", "/api/forum/post",
    signed("post", FM, PRIV_B64, {"community": "lobby", "title": "T"*500, "body": "x"}))

# N24 extra unknown signed fields (must still verify)
st, b = req("POST", "/api/forum/vote",
            signed("vote", FM, PRIV_B64, {"target_type": "post", "target_id": PID, "value": 1,
                                         "mystery_field": "surprise", "nested": {"a": 1}}))
d = jparse(b)
report("extra unknown signed fields ignored (still verifies)", st == 200 and d and d.get("ok"),
       f"status={st} body={b[:120]}")

# N25 numeric timestamp (not string)
st, b = req("POST", "/api/forum/vote",
            signed("vote", FM, PRIV_B64, {"target_type": "post", "target_id": PID, "value": 1},
                   ts_ms=int(time.time()*1000)))
d = jparse(b)
report("numeric (int) timestamp accepted", st == 200 and d and d.get("ok"),
       f"status={st} body={b[:120]}")

print("\n================ SUMMARY ================")
fails = [r for r in RESULTS if not r[1]]
for n, ok, det in RESULTS:
    print(("PASS" if ok else "FAIL"), "-", n)
print(f"\n{len(RESULTS)-len(fails)}/{len(RESULTS)} passed, {len(fails)} failed")
sys.exit(1 if fails else 0)
