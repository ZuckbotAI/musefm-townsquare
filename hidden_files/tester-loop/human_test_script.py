#!/usr/bin/env python3
"""Human tester v2: normal flows + mischief. Robust session handling:
- login with allow_redirects=False; decode Set-Cookie directly to verify fm_id
- retry ONLY on 429 (peek-only, free); a 302 without fm_id is an anomaly -> abort
- carry the session cookie via explicit Cookie header (no jar subtleties)
- log everything to human_run2.log (file) as well as stdout
"""
import requests, re, io, json, time, datetime, os, base64

BASE = "http://127.0.0.1:8473"
HANDLE = "ht_0036"
PW = "testpass123456"
HERE = os.path.dirname(os.path.abspath(__file__))
LOGF = open(os.path.join(HERE, "human_run2.log"), "a")
findings = []

def note(kind, desc):
    line = f"{kind}: {desc}"
    findings.append(line)
    print(line, flush=True)
    LOGF.write(line + "\n"); LOGF.flush()

def decode_session_cookie(raw):
    try:
        body = raw.split(".")[0]
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body))
    except Exception as e:
        return {"_decode_error": str(e)}

def jresp(r):
    ct = r.headers.get("Content-Type", "")
    if ct.startswith("application/json"):
        try: return r.json()
        except Exception: return {"_raw": r.text[:120]}
    return {"_raw": r.text[:120], "_ct": ct[:40]}

# ---------- login ----------
sess_cookie = None
for i in range(40):
    r = requests.post(BASE + "/login", data={"handle": HANDLE, "password": PW},
                      timeout=15, allow_redirects=False)
    if r.status_code == 429:
        ra = int(r.headers.get("Retry-After", "60"))
        print(f"[{datetime.datetime.utcnow():%H:%M:%S}] login 429, Retry-After={ra}; sleep", flush=True)
        time.sleep(min(ra + 5, 300))
        continue
    if r.status_code == 302:
        raw = r.headers.get("Set-Cookie", "")
        sval = raw.split("session=")[1].split(";")[0] if "session=" in raw else ""
        payload = decode_session_cookie(sval)
        note("INFO", f"login 302; session payload keys={sorted(payload.keys())}")
        if payload.get("fm_id"):
            sess_cookie = sval
            note("PASS", f"login as {HANDLE}: session has fm_id={payload['fm_id']}")
            break
        else:
            note("P1", f"ANOMALY: login 302 but session cookie lacks fm_id: {payload}")
            raise SystemExit(2)
    note("FAIL", f"login attempt {i}: unexpected {r.status_code}")
    raise SystemExit(1)

s = requests.Session()
s.headers.update({"Cookie": "session=" + sess_cookie})

def get_tok():
    r = s.get(BASE + "/", timeout=10)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    return m.group(1) if m else None

tok = get_tok()
note("PASS" if tok else "FAIL", f"csrf token after explicit-cookie login: {'OK' if tok else 'MISSING'}")
if not tok:
    raise SystemExit(1)

img = Image.new("RGB", (64, 64), color=(255, 120, 40))

# ---------- NORMAL: post ----------
r = s.post(BASE + "/submit", data={"csrf_token": tok, "community": "lobby",
    "title": "Hello from a human tester", "body": "This is my first post. Testing the forum as a real person would.", "flair": "discussion"}, timeout=15)
m = re.search(r"/post/(\d+)", r.url) or re.search(r"/c/\w+/post/(\d+)", r.text)
pid = m.group(1) if m else None
note("PASS" if pid else "FAIL", f"submit post -> {r.status_code}, pid={pid}, url={r.url[:90]}")

# ---------- NORMAL: comment (+ image) ----------
cid = None
if pid:
    r = s.post(BASE + f"/post/{pid}/comment", data={"csrf_token": tok, "body": "First! Great post, me."}, timeout=15)
    note("PASS" if r.status_code in (200, 302) else "FAIL", f"comment POST -> {r.status_code}")
    r2 = s.get(BASE + f"/c/lobby/post/{pid}", timeout=15)
    note("PASS" if "First! Great post" in r2.text else "FAIL", "comment visible on thread")
    cm = re.search(r'data-comment-id="(\d+)"', r2.text) or re.search(r'comment/(\d+)/edit', r2.text)
    cid = cm.group(1) if cm else None
    print("   cid:", cid, flush=True)
    ibuf = io.BytesIO(); img.save(ibuf, format="PNG"); ibuf.seek(0)
    r = s.post(BASE + f"/post/{pid}/comment", data={"csrf_token": tok},
               files={"body": (None, "reply with a pic"), "image_file": ("reply.png", ibuf, "image/png")}, timeout=30)
    note("PASS" if r.status_code in (200, 302) else "CHECK", f"comment+image attach -> {r.status_code}")

# ---------- NORMAL: vote + double-vote ----------
if pid:
    r = s.post(BASE + "/vote", json={"csrf_token": tok, "target_id": int(pid), "target_type": "post", "value": 1}, timeout=15)
    j = jresp(r)
    note("PASS" if j.get("ok") else "FAIL", f"vote JSON -> {r.status_code} {j}")
    r = s.post(BASE + "/vote", json={"csrf_token": tok, "target_id": int(pid), "target_type": "post", "value": 1}, timeout=15)
    j2 = jresp(r)
    same = j2.get("my_vote") == j.get("my_vote") and j2.get("score") == j.get("score")
    note("PASS" if same else "CHECK", f"double up-vote idempotent: {j} -> {j2}")

# ---------- NORMAL: react ----------
if pid:
    r = s.post(BASE + "/fb_react", data={"csrf_token": tok, "target_type": "post", "target_id": pid, "reaction": "heart"}, timeout=15)
    note("PASS" if r.status_code in (200, 302) else "CHECK", f"fb_react -> {r.status_code} url={r.url[:80]}")

# ---------- NORMAL: photo upload ----------
buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
r = s.post(BASE + "/photos/upload", data={"csrf_token": tok, "title": "test orb pic", "caption": "human tester upload"},
           files={"photo": ("ht_0036.png", buf, "image/png")}, timeout=30)
note("PASS" if r.status_code in (200, 302) and "login" not in r.url else "FAIL",
     f"photo upload (field=photo) -> {r.status_code}, url={r.url[:90]}")
buf2 = io.BytesIO(); img.save(buf2, format="PNG"); buf2.seek(0)
r = s.post(BASE + "/photos/upload", data={"csrf_token": tok, "title": "wrong field"},
           files={"file": ("x.png", buf2, "image/png")}, timeout=30)
note("PASS" if r.status_code == 400 and "pick an image" in r.text.lower() else "CHECK",
     f"upload w/ wrong field name -> {r.status_code} (expect 400 'pick an image file')")

# ---------- NORMAL: shorts / episode / viewport ----------
r = s.get(BASE + "/musefm/shorts", timeout=15, allow_redirects=False)
note("PASS" if r.status_code in (301, 302) else "FAIL", f"/musefm/shorts -> {r.status_code} (expect redirect)")
r = s.get(BASE + "/episodes", timeout=15)
slugs = re.findall(r'/episodes/([a-z0-9\-_]+)"', r.text)
if slugs:
    r = s.get(BASE + f"/episodes/{slugs[0]}", timeout=15)
    note("PASS" if r.status_code == 200 and re.search(r'<audio|<video', r.text) else "CHECK",
         f"episode /episodes/{slugs[0]} -> {r.status_code}")

note("INFO", "--- mischief begins ---")

# M1: script tag in title
r = s.post(BASE + "/submit", data={"csrf_token": tok, "community": "lobby",
    "title": "<script>alert('xss')</script>", "body": "xss test", "flair": "discussion"}, timeout=15)
m = re.search(r"/post/(\d+)", r.url) or re.search(r"/c/\w+/post/(\d+)", r.text)
xpid = m.group(1) if m else None
if xpid:
    r2 = s.get(BASE + f"/c/lobby/post/{xpid}", timeout=15)
    raw_script = "<script>alert('xss')</script>" in r2.text
    escaped = "&lt;script&gt;" in r2.text
    note("P0" if raw_script else ("PASS" if escaped else "CHECK"),
         f"M1 script-in-title: raw reflected={raw_script}, escaped={escaped}")
else:
    note("INFO", f"M1: script-title post not created -> {r.status_code} {r.url[:60]}")

# M2: emoji flood
if pid:
    r = s.post(BASE + f"/post/{pid}/comment", data={"csrf_token": tok, "body": "🔥" * 500}, timeout=15)
    note("PASS" if r.status_code in (200, 302) else "CHECK", f"M2 500-emoji comment -> {r.status_code}")

# M3: 10k accepted / 10001 rejected
r = s.post(BASE + "/submit", data={"csrf_token": tok, "community": "lobby",
    "title": "ten k test", "body": "A" * 10000, "flair": "discussion"}, timeout=20)
m = re.search(r"/post/(\d+)", r.url) or re.search(r"/c/\w+/post/(\d+)", r.text)
note("PASS" if m else "FAIL", f"M3 10k-char body -> {r.status_code}, accepted={bool(m)} (expect accepted)")
r = s.post(BASE + "/submit", data={"csrf_token": tok, "community": "lobby",
    "title": "ten k plus one", "body": "A" * 10001, "flair": "discussion"}, timeout=20)
note("PASS" if r.status_code == 400 and "body too long" in r.text.lower() else "FAIL",
     f"M3b 10001-char body -> {r.status_code} (expect 400 'body too long')")

# M4: weird schemes stay inert
r = s.post(BASE + "/submit", data={"csrf_token": tok, "community": "lobby",
    "title": "weird urls", "body": "links: javascript:alert(1) and data:text/html,<b>x</b> and ftp://example.com/f", "flair": "discussion"}, timeout=15)
m = re.search(r"/post/(\d+)", r.url) or re.search(r"/c/\w+/post/(\d+)", r.text)
wpid = m.group(1) if m else None
if wpid:
    r2 = s.get(BASE + f"/c/lobby/post/{wpid}", timeout=15)
    bad = re.findall(r'href="(javascript:[^"]*|data:[^"]*|ftp:[^"]*)"', r2.text)
    note("P1" if bad else "PASS", f"M4 weird-scheme hrefs: {bad if bad else 'none — inert as expected'}")
else:
    note("INFO", f"M4: weird-url post not created -> {r.status_code} {r.url[:60]}")

# M5: 10 rapid alternating votes
if pid:
    codes = []
    for i in range(10):
        r = s.post(BASE + "/vote", json={"csrf_token": tok, "target_id": int(pid), "target_type": "post", "value": 1 if i % 2 == 0 else -1}, timeout=15)
        codes.append(r.status_code)
    note("PASS" if all(c == 200 for c in codes) else "CHECK", f"M5 10 rapid alternating votes -> {codes}")

# M6: comment edit — huge body + normal edit
if cid:
    r = s.post(BASE + "/comment/edit", data={"csrf_token": tok, "target_type": "comment", "target_id": cid, "body": "B" * 20000}, timeout=20)
    note("INFO", f"M6 edit comment w/ 20k body -> {r.status_code} {r.text[:160]}")
    r = s.post(BASE + "/comment/edit", json={"csrf_token": tok, "target_type": "comment", "target_id": int(cid), "body": "edited: still me"}, timeout=20)
    j = jresp(r)
    note("PASS" if j.get("ok") else "CHECK", f"M6b normal comment edit (JSON) -> {r.status_code} {j}")
else:
    note("CHECK", "M6 skipped: no cid")

# M7: empty post / whitespace comment
r = s.post(BASE + "/submit", data={"csrf_token": tok, "community": "lobby", "title": "", "body": "", "flair": "discussion"}, timeout=15)
note("PASS" if r.status_code == 400 and "title required" in r.text.lower() else "FAIL",
     f"M7 empty post -> {r.status_code} (expect 400 'title required')")
if pid:
    r = s.post(BASE + f"/post/{pid}/comment", data={"csrf_token": tok, "body": "   "}, timeout=15)
    note("PASS" if r.status_code == 400 else "CHECK", f"M7b whitespace-only comment -> {r.status_code} (expect 400)")

# M9: vote without csrf -> 403
if pid:
    r = s.post(BASE + "/vote", json={"target_id": int(pid), "target_type": "post", "value": 1}, timeout=15)
    note("PASS" if r.status_code == 403 else "FAIL", f"M9 vote w/o csrf -> {r.status_code} (expect 403)")

# M10: anonymous vote -> 401
s3 = requests.Session()
r = s3.post(BASE + "/vote", json={"csrf_token": "x", "target_id": 1, "target_type": "post", "value": 1}, timeout=15)
note("PASS" if r.status_code == 401 else "CHECK", f"M10 anon vote -> {r.status_code} (expect 401)")

with open(os.path.join(HERE, "human_findings.txt"), "w") as f:
    f.write("\n".join(findings) + "\n")
LOGF.close()
print("DONE", flush=True)
