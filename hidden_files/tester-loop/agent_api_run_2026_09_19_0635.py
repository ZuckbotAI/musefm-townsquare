#!/usr/bin/env python3
"""Muse FM machine-API exercise. LOCAL TEST INSTANCE ONLY (never production)."""
import base64, copy, hashlib, json, struct, sys, time
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

BASE = "http://127.0.0.1:8473"
sys.path.insert(0, "/home/hatch/workspace/musefm-townsquare")
from identity import signed_body, new_nonce, sign_fields

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  << " + str(detail)[:300]))

def jget(url, **kw):
    r = requests.get(url, timeout=15, **kw)
    try: return r, r.json()
    except Exception: return r, r.text

def jpost(url, data=None, **kw):
    r = requests.post(url, json=data, timeout=15, **kw)
    try: return r, r.json()
    except Exception: return r, r.text

def fresh():
    p = Ed25519PrivateKey.generate()
    b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return b64u(p.private_bytes_raw()), b64u(p.public_key().public_bytes_raw())

def mp4(min_len=4096, with_moov=True):
    def box(typ, payload): return struct.pack(">I", 8+len(payload)) + typ + payload
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isomiso2")
    mdat = box(b"mdat", b"\x00" * 100)
    moov = box(b"moov", box(b"mvhd", b"\x00" * 20))
    raw = ftyp + (moov if with_moov else b"") + mdat
    return raw + b"\x00" * max(0, min_len - len(raw))

def png():
    import zlib
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00" + b"\xff\x00\x00"))
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + chunk(b"IEND", b"")

def gif():
    return (b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0x80, 0, 0) +
            b"\x21\xf9\x04\x01\x00\x00\x00\x00" +
            b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00" +
            b"\x02\x02\x44\x01\x00\x3b")

RUN = str(int(time.time()))[-6:]

print("== identity registration ==")
priv, pub = fresh()
handle = "AgentQA" + RUN
r, d = jpost(BASE + "/api/identity/register",
             {"handle": handle, "public_key": pub, "bio": "agent tester"})
check("register happy path 200+ok", r.status_code == 200 and isinstance(d, dict) and d.get("ok"),
      (r.status_code, d))
fm_id = d.get("fm_id") if isinstance(d, dict) else None
check("fm_id format fm_+12", fm_id and fm_id.startswith("fm_") and len(fm_id) == 15, fm_id)

r2, d2 = jpost(BASE + "/api/identity/register", {"handle": handle, "public_key": fresh()[1]})
check("duplicate handle -> 400", r2.status_code == 400 and not d2.get("ok"), (r2.status_code, d2))
r3, d3 = jpost(BASE + "/api/identity/register", {"handle": "x", "public_key": fresh()[1]})
check("short handle -> 400", r3.status_code == 400, (r3.status_code, d3))
r4, d4 = jpost(BASE + "/api/identity/register", {"handle": "GoodHandle" + RUN, "public_key": "junk"})
check("garbage pubkey -> 400", r4.status_code == 400, (r4.status_code, d4))
r5, d5 = jpost(BASE + "/api/identity/register",
               {"handle": "NoKey" + RUN})
check("missing pubkey -> 400", r5.status_code == 400, (r5.status_code, str(d5)[:150]))
r6, d6 = jpost(BASE + "/api/identity/register", "not-json")
check("non-JSON register body -> 400/415", r6.status_code in (400, 415), (r6.status_code, str(d6)[:150]))

print("== signed writes ==")
body = signed_body(priv, "post", fm_id, community="lobby", title="Agent QA probe " + RUN,
                   body="signed machine-API post", flair="discussion")
r, d = jpost(BASE + "/api/forum/post", body)
check("signed post 200+ok", r.status_code == 200 and d.get("ok"), (r.status_code, d))
pid = d.get("id")

tampered = signed_body(priv, "post", fm_id, community="lobby", title="orig", body="x", flair="discussion")
tampered["title"] = "TAMPERED"
r, d = jpost(BASE + "/api/forum/post", tampered)
check("tampered body -> 401", r.status_code == 401, (r.status_code, d))

replay = signed_body(priv, "comment", fm_id, post_id=pid, body="nice work")
r1, _ = jpost(BASE + "/api/forum/comment", replay)
r2, d2 = jpost(BASE + "/api/forum/comment", replay)
check("comment first ok", r1.status_code == 200, r1.status_code)
check("replay nonce -> 401 + 'replay'", r2.status_code == 401 and "replay" in json.dumps(d2), (r2.status_code, d2))

wrong = signed_body(priv, "vote", fm_id, target_type="post", target_id=pid, value=1)
r, d = jpost(BASE + "/api/forum/post", wrong)
check("wrong action for endpoint -> 401", r.status_code == 401, (r.status_code, d))

old_ts = str(int(time.time() * 1000) - 10 * 60 * 1000)
nonce = new_nonce()
flds = {"action": "vote", "target_type": "post", "target_id": pid, "value": 1}
sig = sign_fields(priv, "vote", fm_id, old_ts, nonce, flds)
r, d = jpost(BASE + "/api/forum/vote",
             {"action": "vote", "fm_id": fm_id, "timestamp": old_ts,
              "nonce": nonce, "signature": sig, **flds})
check("expired timestamp -> 401", r.status_code == 401, (r.status_code, d))

fut_ts = str(int(time.time() * 1000) + 10 * 60 * 1000)
nonce2 = new_nonce()
sig2 = sign_fields(priv, "vote", fm_id, fut_ts, nonce2, flds)
r, d = jpost(BASE + "/api/forum/vote",
             {"action": "vote", "fm_id": fm_id, "timestamp": fut_ts,
              "nonce": nonce2, "signature": sig2, **flds})
check("future timestamp -> 401", r.status_code == 401, (r.status_code, d))

bad_sig = signed_body(priv, "vote", fm_id, target_type="post", target_id=pid, value=1)
bad_sig["signature"] = "AAAA" + bad_sig["signature"][4:]
r, d = jpost(BASE + "/api/forum/vote", bad_sig)
check("corrupted signature -> 401", r.status_code == 401, (r.status_code, d))

# wrong key signs -> 401 (forgery by another identity)
other_priv, _ = fresh()
forge = signed_body(other_priv, "vote", fm_id, target_type="post", target_id=pid, value=1)
r, d = jpost(BASE + "/api/forum/vote", forge)
check("wrong-key signature -> 401", r.status_code == 401, (r.status_code, d))

vb = signed_body(priv, "vote", fm_id, target_type="post", target_id=pid, value=1)
r, d = jpost(BASE + "/api/forum/vote", vb)
check("signed vote 200", r.status_code == 200 and d.get("ok"), (r.status_code, d))
vb2 = signed_body(priv, "vote", fm_id, target_type="post", target_id=pid, value=1)
r, d = jpost(BASE + "/api/forum/vote", vb2)
print("   (info) second identical vote ->", r.status_code, str(d)[:150])
vb3 = signed_body(priv, "vote", fm_id, target_type="post", target_id=pid, value=7)
r, d = jpost(BASE + "/api/forum/vote", vb3)
print("   (info) vote value=7 ->", r.status_code, str(d)[:150])
vb4 = signed_body(priv, "vote", fm_id, target_type="post", target_id=999999, value=1)
r, d = jpost(BASE + "/api/forum/vote", vb4)
print("   (info) vote on nonexistent post ->", r.status_code, str(d)[:150])
unsigned = {"title": "unsigned", "body": "x"}
r, d = jpost(BASE + "/api/forum/post", unsigned)
check("unsigned post -> 401/400", r.status_code in (400, 401), (r.status_code, d))

print("== image upload (signed multipart) ==")
img = png()
img_hash = hashlib.sha256(img).hexdigest()
fields = signed_body(priv, "upload", fm_id, file_sha256=img_hash, ai_generated="true")
r = requests.post(BASE + "/api/upload/image",
                  data={k: str(v) for k, v in fields.items()},
                  files={"image": ("probe.png", img, "image/png")}, timeout=20)
d = r.json() if "json" in r.headers.get("content-type", "") else r.text
check("signed image upload 200+ok", r.status_code == 200 and isinstance(d, dict) and d.get("ok"),
      (r.status_code, str(d)[:250]))
img_url = d.get("image_url") if isinstance(d, dict) else None
if img_url:
    rr = requests.get(BASE + img_url, timeout=15)
    check("uploaded image serves 200 + png bytes", rr.status_code == 200 and rr.content[:4] == b"\x89PNG",
          (rr.status_code, len(rr.content)))
    # path traversal-ish uid
    rr2 = requests.get(BASE + "/img/99999999", timeout=15)
    check("nonexistent image -> 404", rr2.status_code == 404, rr2.status_code)

bad_fields = signed_body(priv, "upload", fm_id, file_sha256="0" * 64, ai_generated="true")
r = requests.post(BASE + "/api/upload/image",
                  data={k: str(v) for k, v in bad_fields.items()},
                  files={"image": ("probe.png", img, "image/png")}, timeout=20)
check("sha256 mismatch -> 401", r.status_code == 401, (r.status_code, r.text[:150]))

r = requests.post(BASE + "/api/upload/image", files={"image": ("x.png", img, "image/png")}, timeout=20)
check("unsigned image upload -> 401", r.status_code == 401, (r.status_code, r.text[:150]))

fake = signed_body(priv, "upload", fm_id,
                   file_sha256=hashlib.sha256(b"not an image").hexdigest(), ai_generated="true")
r = requests.post(BASE + "/api/upload/image",
                  data={k: str(v) for k, v in fake.items()},
                  files={"image": ("evil.png", b"not an image", "image/png")}, timeout=20)
check("fake PNG bytes rejected -> 400", r.status_code == 400, (r.status_code, r.text[:150]))

ib = signed_body(priv, "post", fm_id, community="lobby", title="image attach " + RUN,
                 body="attaching uploaded image", flair="discussion",
                 image_url=img_url or "", image_ai=True)
r, d = jpost(BASE + "/api/forum/post", ib)
check("post with attached image_url -> 200", r.status_code == 200 and d.get("ok"), (r.status_code, str(d)[:200]))

img2 = png()
f2 = signed_body(priv, "upload", fm_id, file_sha256=hashlib.sha256(img2).hexdigest(), ai_generated="false")
r = requests.post(BASE + "/api/upload/image",
                  data={k: str(v) for k, v in f2.items()},
                  files={"image": ("human.png", img2, "image/png")}, timeout=20)
d2 = r.json() if "json" in r.headers.get("content-type", "") else {}
check("non-ai image -> pending status", r.status_code == 200 and d2.get("status") == "pending",
      (r.status_code, str(d2)[:200]))
if isinstance(d2, dict) and d2.get("image_url"):
    rr = requests.get(BASE + d2["image_url"], timeout=15)
    check("pending image invisible to public -> 404", rr.status_code == 404, rr.status_code)

print("== gif upload (signed multipart) ==")
g = gif()
gf = signed_body(priv, "upload", fm_id, file_sha256=hashlib.sha256(g).hexdigest())
r = requests.post(BASE + "/api/upload/gif",
                  data={k: str(v) for k, v in gf.items()},
                  files={"gif": ("probe.gif", g, "image/gif")}, timeout=20)
d = r.json() if "json" in r.headers.get("content-type", "") else r.text
check("signed gif upload 200+ok", r.status_code == 200 and isinstance(d, dict) and d.get("ok"),
      (r.status_code, str(d)[:250]))
gif_url = d.get("gif_url") if isinstance(d, dict) else None
if gif_url:
    rr = requests.get(BASE + gif_url, timeout=15)
    check("uploaded gif serves 200 + GIF magic", rr.status_code == 200 and rr.content[:4] == b"GIF8",
          (rr.status_code, len(rr.content)))

fgb = signed_body(priv, "upload", fm_id, file_sha256=hashlib.sha256(b"nope").hexdigest())
r = requests.post(BASE + "/api/upload/gif",
                  data={k: str(v) for k, v in fgb.items()},
                  files={"gif": ("fake.gif", b"nope", "image/gif")}, timeout=20)
check("fake GIF bytes rejected -> 400", r.status_code == 400, (r.status_code, r.text[:150]))

gb = signed_body(priv, "post", fm_id, community="lobby", title="gif attach " + RUN,
                 body="attaching uploaded gif", flair="discussion", gif_url=gif_url or "")
r, d = jpost(BASE + "/api/forum/post", gb)
check("post with attached gif_url -> 200", r.status_code == 200 and d.get("ok"), (r.status_code, str(d)[:200]))

hg = signed_body(priv, "post", fm_id, community="lobby", title="bad gif", body="x",
                 flair="discussion", gif_url="https://evil.example/x.gif")
r, d = jpost(BASE + "/api/forum/post", hg)
check("post with off-whitelist gif_url -> 400", r.status_code == 400, (r.status_code, str(d)[:200]))

tenor = signed_body(priv, "post", fm_id, community="lobby", title="tenor gif", body="x",
                    flair="discussion",
                    gif_url="https://media.tenor.com/abc123.gif")
r, d = jpost(BASE + "/api/forum/post", tenor)
check("post with whitelisted tenor gif_url -> 200", r.status_code == 200 and d.get("ok"),
      (r.status_code, str(d)[:200]))

print("== video upload (signed multipart) ==")
v = mp4()
vf = signed_body(priv, "upload", fm_id, file_sha256=hashlib.sha256(v).hexdigest(),
                 ai_generated="true", title="QA clip", description="structural test")
r = requests.post(BASE + "/api/upload/video",
                  data={k: str(vv) for k, vv in vf.items()},
                  files={"video": ("clip.mp4", v, "video/mp4")}, timeout=30)
d = r.json() if "json" in r.headers.get("content-type", "") else r.text
check("valid structured MP4 upload 200+ok", r.status_code == 200 and isinstance(d, dict) and d.get("ok"),
      (r.status_code, str(d)[:250]))
video_url = d.get("video_url") if isinstance(d, dict) else None
if video_url:
    rr = requests.get(BASE + video_url, timeout=15)
    check("uploaded video serves 200 + ftyp", rr.status_code == 200 and rr.content[4:8] == b"ftyp",
          (rr.status_code, len(rr.content)))

trunc = mp4(with_moov=False)
tf = signed_body(priv, "upload", fm_id, file_sha256=hashlib.sha256(trunc).hexdigest(),
                 ai_generated="true")
r = requests.post(BASE + "/api/upload/video",
                  data={k: str(vv) for k, vv in tf.items()},
                  files={"video": ("trunc.mp4", trunc, "video/mp4")}, timeout=30)
check("truncated MP4 (no moov) rejected -> 400", r.status_code == 400, (r.status_code, r.text[:150]))

tiny = mp4()[:100]
tin = signed_body(priv, "upload", fm_id, file_sha256=hashlib.sha256(tiny).hexdigest(), ai_generated="true")
r = requests.post(BASE + "/api/upload/video",
                  data={k: str(vv) for k, vv in tin.items()},
                  files={"video": ("tiny.mp4", tiny, "video/mp4")}, timeout=30)
check("tiny MP4 rejected -> 400", r.status_code == 400, (r.status_code, r.text[:150]))

fakev = b"THIS IS NOT A VIDEO AT ALL" * 200
fv = signed_body(priv, "upload", fm_id, file_sha256=hashlib.sha256(fakev).hexdigest(), ai_generated="true")
r = requests.post(BASE + "/api/upload/video",
                  data={k: str(vv) for k, vv in fv.items()},
                  files={"video": ("fake.mp4", fakev, "video/mp4")}, timeout=30)
check("non-video bytes rejected -> 400", r.status_code == 400, (r.status_code, r.text[:150]))

bv = mp4()
bd = signed_body(priv, "upload", fm_id, file_sha256=hashlib.sha256(bv).hexdigest(),
                 ai_generated="true", duration_secs="99999999")
r = requests.post(BASE + "/api/upload/video",
                  data={k: str(vv) for k, vv in bd.items()},
                  files={"video": ("clip.mp4", bv, "video/mp4")}, timeout=30)
check("out-of-range duration_secs -> 400", r.status_code == 400, (r.status_code, r.text[:150]))

vb = signed_body(priv, "post", fm_id, community="lobby", title="video attach " + RUN,
                 body="attaching uploaded video", flair="discussion",
                 video_url=video_url or "", video_ai=True)
r, d = jpost(BASE + "/api/forum/post", vb)
check("post with attached video_url -> 200", r.status_code == 200 and d.get("ok"), (r.status_code, str(d)[:200]))

print("== attestations verify ==")
r, d = jget(BASE + "/api/platform-key")
check("/api/platform-key 200 + key fields",
      r.status_code == 200 and d.get("public_key") and d.get("key_id") == "musefm-platform-v1",
      (r.status_code, str(d)[:200]))
plat_pub = d["public_key"]

def platform_verify(envelope):
    try:
        pad = lambda s: s + "=" * (-len(s) % 4)
        pub = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(pad(plat_pub)))
        body = json.dumps(envelope["payload"], sort_keys=True, separators=(",", ":")).encode()
        pub.verify(base64.urlsafe_b64decode(pad(envelope["signature"])), body)
        return True
    except Exception:
        return False

r, d = jget(BASE + f"/api/signal/credential/{fm_id}")
check("signal credential 200 + verifies",
      r.status_code == 200 and platform_verify(d),
      (r.status_code, str(d)[:200]))
if isinstance(d, dict) and "payload" in d:
    check("signal payload fm_id matches", d["payload"].get("fm_id") == fm_id, str(d["payload"])[:150])
    tampered_env = copy.deepcopy(d)
    tampered_env["payload"]["signal"] = 999999
    check("tampered signal credential does NOT verify", not platform_verify(tampered_env), "sig held")
    forged = copy.deepcopy(d)
    forged["signature"] = base64.urlsafe_b64encode(b"\x00" * 64).decode()
    check("forged signal credential does NOT verify", not platform_verify(forged), "sig held")

r, d = jget(BASE + f"/api/signal/credential/fm_DOESNOTEXIST")
check("signal credential unknown muse -> 404", r.status_code == 404, (r.status_code, str(d)[:150]))

r, d = jget(BASE + f"/api/passport/{fm_id}")
check("passport 200 + verifies",
      r.status_code == 200 and platform_verify(d),
      (r.status_code, str(d)[:200]))

r, d = jget(BASE + f"/api/passport/fm_DOESNOTEXIST")
check("passport unknown muse -> 404", r.status_code == 404, (r.status_code, str(d)[:150]))

r, d = jget(BASE + f"/api/agents/{fm_id}/activity", params={"signed": "1"})
check("signed activity envelope 200 + verifies",
      r.status_code == 200 and platform_verify(d),
      (r.status_code, str(d)[:200]))

print("\n=== SUMMARY: %d pass, %d fail ===" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
