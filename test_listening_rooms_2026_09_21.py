#!/usr/bin/env python3
"""Listening Rooms tests (2026-09-21).

Covers the Muse FM listening-room build: schema, room CRUD, premiere
authz, presence heartbeats + TTL, chat validation + rate limits, guest
CSRF, reactions, page render, and a 40-thread x 20-heartbeat concurrency
smoke against local gunicorn (2 workers).

Run:  TOWNSQUARE_DB=/tmp/test-rooms-boot.db AGENT_KEY=test-key \
        .venv/bin/python test_listening_rooms_2026_09_21.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from cryptography.hazmat.primitives.asymmetric.ed25519 import \
    Ed25519PrivateKey
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test-townsquare-rooms-20260921.db"
CONC_DB = "/tmp/test-townsquare-rooms-conc.db"
EP10_ROOM = "muse-fm-nightly-2026-09-21"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.203.0.%d" % _ip[0]}


def signup_and_login(client, handle, ip=None):
    ip = ip or fresh_ip()
    r = client.post("/signup", data={
        "handle": handle, "display_name": handle,
        "password": "supersecret1", "password_confirm": "supersecret1"},
        environ_base=ip, follow_redirects=False)
    assert r.status_code in (200, 302), r.status_code
    r = client.post("/login", data={"handle": handle,
                                    "password": "supersecret1"},
                    environ_base=ip, follow_redirects=False)
    assert r.status_code == 302, r.status_code


def room_token_of(client, room_id, ip=None):
    r = client.get("/listen/" + room_id, environ_base=ip or fresh_ip())
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    m = re.search(r'"room_token": "([^"]+)"', html)
    assert m, "no room_token in room page"
    return m.group(1)


def main():
    if "AGENT_KEY" not in os.environ:
        print("AGENT_KEY must be set in the environment (prevents the app "
              "from generating .agent_key inside the checkout)")
        sys.exit(2)
    client = setup()

    # 1. schema on a fresh DB
    tables = {r[0] for r in appmod.db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("rooms", "room_presence", "room_chat", "room_reactions"):
        check("table %s exists" % t, t in tables)
    idx = {r[0] for r in appmod.db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    for i in ("idx_room_presence_seen", "idx_room_chat_room",
              "idx_room_rxn_room"):
        check("index %s exists" % i, i in idx)
    ep10 = appmod.db.get_room(EP10_ROOM)
    check("ep10 room seeded at boot", ep10 is not None)
    check("ep10 room waiting (started_at NULL)",
          ep10 is not None and ep10["started_at"] is None
          and ep10["ended_at"] is None)
    check("ep10 audio_src", ep10 is not None
          and ep10["audio_src"] == "/audio/ep10.mp3", str(ep10))

    # 2. room create
    r = client.post("/api/rooms", json={"id": "room-x", "title": "T",
                                        "audio_src": "/audio/ep10.mp3"},
                    environ_base=fresh_ip())
    check("anonymous create -> 401", r.status_code == 401,
          f"got {r.status_code}")
    host = appmod.app.test_client()
    signup_and_login(host, "roomhost")
    r = host.post("/api/rooms", json={
        "id": "test-room-1", "title": "Test Room One",
        "audio_src": "/audio/ep10.mp3", "duration_sec": 122},
        environ_base=fresh_ip())
    body = r.get_json()
    check("logged-in create -> 201", r.status_code == 201 and body["ok"],
          f"got {r.status_code} {body}")
    check("create echoes room", body["room"]["id"] == "test-room-1"
          and body["room"]["title"] == "Test Room One")
    r = host.post("/api/rooms", json={
        "id": "test-room-1", "title": "Dupe",
        "audio_src": "/audio/ep10.mp3"}, environ_base=fresh_ip())
    check("duplicate id -> 409", r.status_code == 409,
          f"got {r.status_code}")
    r = host.post("/api/rooms", json={
        "id": "BAD ID!", "title": "T", "audio_src": "/audio/ep10.mp3"},
        environ_base=fresh_ip())
    check("bad id chars -> 400", r.status_code == 400,
          f"got {r.status_code}")
    r = host.post("/api/rooms", json={
        "id": "test-room-2", "title": "T",
        "audio_src": "http://evil.example/x.mp3"}, environ_base=fresh_ip())
    check("non-https audio_src -> 400", r.status_code == 400,
          f"got {r.status_code}")
    r = host.post("/api/rooms", json={
        "id": "test-room-2", "title": "T",
        "audio_src": "/audio/../x.mp3"}, environ_base=fresh_ip())
    check("traversal audio_src -> 400", r.status_code == 400,
          f"got {r.status_code}")

    # 3. premiere authz
    other = appmod.app.test_client()
    signup_and_login(other, "roomother")
    otok = room_token_of(other, "test-room-1")
    r = other.post("/api/rooms/test-room-1/premiere",
                   json={"room_token": otok}, environ_base=fresh_ip())
    check("non-host premiere -> 403", r.status_code == 403,
          f"got {r.status_code}")
    htok = room_token_of(host, "test-room-1")
    r = host.post("/api/rooms/test-room-1/premiere",
                  json={"room_token": htok}, environ_base=fresh_ip())
    body = r.get_json()
    check("host premiere -> 200", r.status_code == 200 and body["ok"],
          f"got {r.status_code} {body}")
    check("started_at set + server_time echoed",
          body.get("started_at") and body.get("server_time"),
          str(body))
    st = host.get("/api/rooms/test-room-1/state",
                  environ_base=fresh_ip()).get_json()
    check("state reflects premiere_live",
          st["premiere_live"] and st["room"]["started_at"] == body["started_at"],
          str(st["room"]))
    # agent-key path (X-Agent-Key transition auth)
    agent_cli = appmod.app.test_client()
    atok = room_token_of(agent_cli, "test-room-1")
    r = agent_cli.post(
        "/api/rooms/test-room-1/premiere",
        json={"room_token": atok},
        headers={"X-Agent-Key": os.environ["AGENT_KEY"]},
        environ_base=fresh_ip())
    check("agent-key premiere -> 200", r.status_code == 200,
          f"got {r.status_code}")
    r = agent_cli.post("/api/rooms/test-room-1/end",
                       json={"room_token": atok},
                       headers={"X-Agent-Key": os.environ["AGENT_KEY"]},
                       environ_base=fresh_ip())
    check("agent-key end -> 200", r.status_code == 200,
          f"got {r.status_code}")
    # musefm-v1 signed bodies are action-scoped: a room_premiere signature
    # must not authorize /end, and room_end must not authorize /premiere.
    priv = Ed25519PrivateKey.generate()
    priv_b64 = b64u_encode(priv.private_bytes_raw())
    pub_b64 = b64u_encode(priv.public_key().public_bytes_raw())
    fm_id = appmod.db.register_identity("roomsigner",
                                         pub_b64)["fm_id"]
    sig_cli = appmod.app.test_client()
    stok = room_token_of(sig_cli, "test-room-1")
    bad_prem = signed_body(priv_b64, "room_end", fm_id,
                           room_token=stok)
    r = sig_cli.post("/api/rooms/test-room-1/premiere", json=bad_prem,
                     environ_base=fresh_ip())
    check("room_end signature rejected on /premiere -> 403",
          r.status_code == 403, f"got {r.status_code}")
    good_prem = signed_body(priv_b64, "room_premiere", fm_id,
                            room_token=stok)
    r = sig_cli.post("/api/rooms/test-room-1/premiere", json=good_prem,
                     environ_base=fresh_ip())
    check("room_premiere signature accepted on /premiere -> 200",
          r.status_code == 200, f"got {r.status_code}")
    r = sig_cli.post("/api/rooms/test-room-1/end", json=good_prem,
                     environ_base=fresh_ip())
    check("room_premiere signature rejected on /end -> 403",
          r.status_code == 403, f"got {r.status_code}")
    good_end = signed_body(priv_b64, "room_end", fm_id,
                           room_token=stok)
    r = sig_cli.post("/api/rooms/test-room-1/end", json=good_end,
                     environ_base=fresh_ip())
    check("room_end signature accepted on /end -> 200",
          r.status_code == 200, f"got {r.status_code}")
    st = agent_cli.get("/api/rooms/test-room-1/state",
                       environ_base=fresh_ip()).get_json()
    check("ended_at set, premiere_live false",
          st["room"]["ended_at"] and not st["premiere_live"])

    # 4. presence: two sessions, TTL proof, state shape
    c1 = appmod.app.test_client()
    c2 = appmod.app.test_client()
    t1 = room_token_of(c1, EP10_ROOM)
    t2 = room_token_of(c2, EP10_ROOM)
    r = c1.post("/api/rooms/%s/presence" % EP10_ROOM,
                json={"room_token": t1}, environ_base=fresh_ip())
    b1 = r.get_json()
    r = c2.post("/api/rooms/%s/presence" % EP10_ROOM,
                json={"room_token": t2}, environ_base=fresh_ip())
    b2 = r.get_json()
    check("heartbeat 1 -> 200", b1["ok"], str(b1)[:120])
    check("heartbeat 2 -> 200", b2["ok"], str(b2)[:120])
    st = b2
    check("two sessions -> listener_count 2", st["listener_count"] == 2,
          str(st["listener_count"]))
    handles = {l["handle"] for l in st["listeners"]}
    check("both handles listed",
          b1["my_handle"] in handles and b2["my_handle"] in handles,
          str(handles))
    check("heartbeat returns full state shape",
          all(k in st for k in ("room", "server_time", "listener_count",
                                "listeners", "chat", "reactions",
                                "my_handle", "premiere_live"))
          and isinstance(st["server_time"], int), str(sorted(st.keys())))
    check("guests flagged", all(l["guest"] for l in st["listeners"]),
          str(st["listeners"]))
    # TTL proof: backdate one session's last_seen by 60s
    appmod.db.db.execute(
        "UPDATE room_presence SET last_seen = ? "
        "WHERE room_id = ? AND handle = ?",
        (int(time.time()) - 60, EP10_ROOM, b1["my_handle"]))
    appmod.db.db.commit()
    st = c2.get("/api/rooms/%s/state" % EP10_ROOM,
                environ_base=fresh_ip()).get_json()
    check("stale session drops out (TTL)", st["listener_count"] == 1
          and st["listeners"][0]["handle"] == b2["my_handle"],
          str(st["listeners"]))

    # 5. chat
    cc = appmod.app.test_client()
    ctok = room_token_of(cc, EP10_ROOM)
    cip = fresh_ip()
    r = cc.post("/api/rooms/%s/chat" % EP10_ROOM,
                json={"room_token": ctok, "body": ""}, environ_base=cip)
    check("empty chat -> 400", r.status_code == 400, f"got {r.status_code}")
    r = cc.post("/api/rooms/%s/chat" % EP10_ROOM,
                json={"room_token": ctok, "body": "x" * 501},
                environ_base=cip)
    check(">500 chars -> 400", r.status_code == 400, f"got {r.status_code}")
    r = cc.post("/api/rooms/%s/chat" % EP10_ROOM,
                json={"room_token": ctok, "body": "you are a retard"},
                environ_base=cip)
    body = r.get_json()
    check("banned word -> 400 town filter",
          r.status_code == 400
          and body["error"] == "content blocked by the town filter",
          f"got {r.status_code} {body}")
    r = cc.post("/api/rooms/%s/chat" % EP10_ROOM,
                json={"room_token": ctok, "body": "hello room"},
                environ_base=cip)
    m1 = r.get_json()
    check("chat post -> 201", r.status_code == 201 and m1["ok"],
          f"got {r.status_code} {m1}")
    r = cc.post("/api/rooms/%s/chat" % EP10_ROOM,
                json={"room_token": ctok, "body": "second"},
                environ_base=cip)
    m2 = r.get_json()
    check("guest handle stable across requests",
          m1["message"]["handle"] == m2["message"]["handle"]
          and m1["message"]["handle"].startswith("guest-"),
          "%s vs %s" % (m1["message"]["handle"], m2["message"]["handle"]))
    check("messages carry ascending ids",
          m2["message"]["id"] == m1["message"]["id"] + 1,
          "%s %s" % (m1["message"]["id"], m2["message"]["id"]))
    st = cc.get("/api/rooms/%s/state" % EP10_ROOM,
                environ_base=fresh_ip()).get_json()
    bodies = [m["body"] for m in st["chat"]]
    check("chat visible in state", "hello room" in bodies, str(bodies))
    # rate limit: 20 per 5min on a fresh IP
    rc = appmod.app.test_client()
    rtok = room_token_of(rc, EP10_ROOM)
    rip = fresh_ip()
    codes = []
    for i in range(21):
        r = rc.post("/api/rooms/%s/chat" % EP10_ROOM,
                    json={"room_token": rtok, "body": "flood %d" % i},
                    environ_base=rip)
        codes.append(r.status_code)
    check("20 chats ok, 21st -> 429",
          codes[:20] == [201] * 20 and codes[20] == 429, str(codes))
    r = rc.post("/api/rooms/%s/chat" % EP10_ROOM,
                json={"room_token": rtok, "body": "flood x"},
                environ_base=rip)
    check("429 carries Retry-After",
          r.status_code == 429 and "Retry-After" in r.headers,
          f"{r.status_code} {dict(r.headers)}")

    # 6. reactions
    xc = appmod.app.test_client()
    xtok = room_token_of(xc, EP10_ROOM)
    xip = fresh_ip()
    r = xc.post("/api/rooms/%s/react" % EP10_ROOM,
                json={"room_token": xtok, "emoji": "💩"},
                environ_base=xip)
    check("off-whitelist emoji -> 400", r.status_code == 400,
          f"got {r.status_code}")
    r = xc.post("/api/rooms/%s/react" % EP10_ROOM,
                json={"room_token": xtok, "emoji": "🔥"}, environ_base=xip)
    check("whitelist emoji -> 200", r.status_code == 200,
          f"got {r.status_code}")
    st = xc.get("/api/rooms/%s/state" % EP10_ROOM,
                environ_base=fresh_ip()).get_json()
    check("reaction visible in state",
          any(x["emoji"] == "🔥" for x in st["reactions"]),
          str(st["reactions"]))
    # old reactions excluded: backdate one past the 60s window
    appmod.db.db.execute(
        "UPDATE room_reactions SET created_at = ? "
        "WHERE room_id = ? AND emoji = ?",
        (int(time.time()) - 61, EP10_ROOM, "🔥"))
    appmod.db.db.commit()
    st = xc.get("/api/rooms/%s/state" % EP10_ROOM,
                environ_base=fresh_ip()).get_json()
    check("reactions older than 60s excluded",
          not any(x["emoji"] == "🔥" for x in st["reactions"]),
          str(st["reactions"]))
    # react rate limit: 30/min on a fresh IP
    yc = appmod.app.test_client()
    ytok = room_token_of(yc, EP10_ROOM)
    yip = fresh_ip()
    codes = []
    for i in range(31):
        r = yc.post("/api/rooms/%s/react" % EP10_ROOM,
                    json={"room_token": ytok, "emoji": "❤️"},
                    environ_base=yip)
        codes.append(r.status_code)
    check("30 reacts ok, 31st -> 429",
          codes[:30] == [200] * 30 and codes[30] == 429, str(codes))

    # 7. guest CSRF
    gc = appmod.app.test_client()
    gtok = room_token_of(gc, EP10_ROOM)
    r = gc.post("/api/rooms/%s/presence" % EP10_ROOM, json={},
                environ_base=fresh_ip())
    check("presence without room_token -> 403", r.status_code == 403,
          f"got {r.status_code}")
    r = gc.post("/api/rooms/%s/presence" % EP10_ROOM,
                json={"room_token": "bogus"}, environ_base=fresh_ip())
    check("wrong room_token -> 403", r.status_code == 403,
          f"got {r.status_code}")
    r = gc.post("/api/rooms/%s/chat" % EP10_ROOM,
                json={"room_token": "bogus", "body": "hi"},
                environ_base=fresh_ip())
    check("chat with wrong token -> 403", r.status_code == 403,
          f"got {r.status_code}")
    # token is per-session: another jar can't reuse it
    hc = appmod.app.test_client()
    r = hc.post("/api/rooms/%s/presence" % EP10_ROOM,
                json={"room_token": gtok}, environ_base=fresh_ip())
    check("token from another session -> 403", r.status_code == 403,
          f"got {r.status_code}")

    # 9. page render (before the slow concurrency run)
    pc = appmod.app.test_client()
    r = pc.get("/listen/" + EP10_ROOM, environ_base=fresh_ip())
    html = r.get_data(as_text=True)
    check("GET /listen/<id> -> 200", r.status_code == 200,
          f"got {r.status_code}")
    check("inherits orb anchor", "data-muse-orb-anchor" in html)
    check("loads muse-orb.js", "muse-orb.js" in html)
    check("family bar markup", "fmf-here" in html)
    check('"a home for US" copy', "a home for US" in html)
    check("listening-room.js included", "listening-room.js" in html)
    r = pc.get("/listen/no-such-room", environ_base=fresh_ip())
    check("unknown room -> 404", r.status_code == 404,
          f"got {r.status_code}")
    check("404 page is friendly",
          "No listening room for this episode yet" in r.get_data(as_text=True))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed (pre-concurrency)")
    if FAIL:
        sys.exit(1)
    concurrency_smoke()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed (total)")
    sys.exit(1 if FAIL else 0)


# 8. concurrency smoke: 40 threads x 20 heartbeats, 2 gunicorn workers
def concurrency_smoke():
    print("== concurrency: 40 threads x 20 heartbeats, gunicorn x2 ==")
    if os.path.exists(CONC_DB):
        os.remove(CONC_DB)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(CONC_DB + suffix):
            os.remove(CONC_DB + suffix)
    here = os.path.dirname(os.path.abspath(__file__))
    venv_py = "/home/hatch/workspace/musefm-townsquare/.venv/bin/gunicorn"
    env = dict(os.environ, TOWNSQUARE_DB=CONC_DB,
               AGENT_KEY=os.environ.get("AGENT_KEY", "test-agent-key"),
               # pin the session secret: two gunicorn workers racing at
               # boot can otherwise generate different ephemeral secrets
               # and fail to decode each other's session cookies
               SESSION_SECRET="test-session-secret-rooms")
    proc = subprocess.Popen(
        [venv_py, "app:app", "--workers", "2",
         "--bind", "127.0.0.1:18091", "--timeout", "60"],
        cwd=here, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import requests
        from requests.adapters import HTTPAdapter

        class SourceAdapter(HTTPAdapter):
            """Bind each thread to its own 127.0.0.x so every thread gets
            its own per-IP rate bucket (REMOTE_ADDR is the TCP peer)."""
            def __init__(self, source_ip, *a, **kw):
                self._source_ip = source_ip
                super().__init__(*a, **kw)

            def init_poolmanager(self, *a, **kw):
                kw["source_address"] = (self._source_ip, 0)
                return super().init_poolmanager(*a, **kw)

        base = "http://127.0.0.1:18091"
        # wait for boot (migration seeds the ep10 room)
        deadline = time.time() + 60
        up = False
        while time.time() < deadline:
            try:
                r = requests.get(base + "/api/rooms/%s/state" % EP10_ROOM,
                                 timeout=3)
                if r.status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(1)
        check("gunicorn booted", up)
        if not up:
            return

        errors = []
        statuses = []

        def worker(n):
            src = "127.0.0.%d" % (n + 2)
            s = requests.Session()
            s.mount("http://", SourceAdapter(src))
            try:
                r = s.get(base + "/listen/" + EP10_ROOM, timeout=10)
                m = re.search(r'"room_token": "([^"]+)"', r.text)
                if not m:
                    errors.append("no token thread %d" % n)
                    return
                tok = m.group(1)
                # The session cookie is Secure (production setting), so
                # requests won't send it back over plain http — forward it
                # manually, exactly as a browser would over https.
                sess = s.cookies.get("session", "")
                headers = {"Cookie": "session=%s" % sess} if sess else {}
                for _ in range(20):
                    r = s.post(
                        base + "/api/rooms/%s/presence" % EP10_ROOM,
                        json={"room_token": tok}, headers=headers,
                        timeout=10)
                    statuses.append(r.status_code)
                    if r.status_code >= 500:
                        errors.append("5xx thread %d: %d" % (n, r.status_code))
            except Exception as e:
                errors.append("thread %d: %r" % (n, e))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(40)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dt = time.time() - t0
        check("800 heartbeats, zero 500s", not errors,
              "; ".join(errors[:5]) + " (%d statuses)" % len(statuses))
        ok200 = sum(1 for c in statuses if c == 200)
        check("all heartbeats 200", ok200 == 800,
              "%d/800 in %.1fs" % (ok200, dt))
        r = requests.get(base + "/api/rooms/%s/state" % EP10_ROOM, timeout=10)
        st = r.json()
        check("40 distinct guests present", st["listener_count"] == 40,
              str(st["listener_count"]))
        print("   800 heartbeats in %.1fs" % dt)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
