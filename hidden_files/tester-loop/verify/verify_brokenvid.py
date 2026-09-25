#!/usr/bin/env python3
"""Genuinely-broken video uploads: magic-ok but no moov. Expect 400. Local only."""
import hashlib, json, os, secrets, sys, urllib.request, urllib.error
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
    boundary = "----b" + secrets.token_hex(8)
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
HANDLE = "brokenvid_" + secrets.token_hex(3)
st, body = req("POST", "/api/identity/register", body={"handle": HANDLE, "public_key": PUB_B64})
FM_ID = (jload(body) or {}).get("fm_id", "")
assert st == 200 and FM_ID.startswith("fm_"), "registration failed: %s %s" % (st, body[:100])
print("registered", HANDLE, FM_ID)

mp4 = open(os.path.join(HERE, "test.mp4"), "rb").read()

cases = {
    # ftyp magic + zeros, 5000 bytes: magic ok, no moov
    "ftyp+zeros 5000B": mp4[:32] + b"\x00" * (5000 - 32),
    # real file with every 'moov' box tag corrupted -> no moov present
    "moov-scrubbed 4295B": mp4.replace(b"moov", b"xxxx"),
    # tiny file below MIN_VIDEO_BYTES
    "tiny ftyp 100B": mp4[:100],
    # random garbage, no magic
    "garbage 5000B": secrets.token_bytes(5000),
}

for name, raw in cases.items():
    flds = signed_body(PRIV_B64, "upload", FM_ID, file_sha256=hashlib.sha256(raw).hexdigest(), ai_generated="true")
    flds = {k: str(v) for k, v in flds.items()}
    st, body = multipart("/api/upload/video", flds, "video", "broken.mp4", raw, "video/mp4")
    d = jload(body) or {}
    verdict = "OK-rejected" if st in (400, 401, 413) else "*** ACCEPTED ***"
    print(f"{name}: -> {st} {str(d.get('error', d.get('status')))[:70]} [{verdict}]")
