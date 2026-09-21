#!/usr/bin/env python3
"""Regression test for tester-loop P2s (2026-09-21 ~15:35 CDT run).

1. Signed /api/forum/react + /api/forum/fb_react burned the per-IP rate
   budget on invalid emoji/reaction/unknown target, despite code comments
   claiming validation happens before counting. 121+ invalid requests ->
   429, locking out legit traffic. Expected now: invalid input 400s
   WITHOUT touching the bucket; a subsequent valid react -> 200.

2. og:url / og:image / twitter:image hardcoded https://musefm.lol on every
   page (templates/base.html). Share links from any non-prod deployment
   advertised prod URLs. Expected now: request-derived host.

3. /fb_react (web) with a missing target_id reported "unknown target"
   (misleading: the param was absent, the target wasn't unknown).
   Expected now: 400 "missing target_id".

Throwaway DB; nothing touches the real townsquare.db. Uncommitted.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from db import Database

TEST_DB = "/tmp/test-testerloop-p2-2026-09-21-1535.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


_ip = [100]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.77.0.%d" % _ip[0]}


def csrf_of(client):
    r = client.get("/")
    html = r.get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    akey = {"X-Agent-Key": appmod.AGENT_KEY}

    # seed one real post to react at (via db, avoids web-form budget)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import \
        Ed25519PrivateKey
    from identity import b64u_encode
    db = appmod.db
    _priv = Ed25519PrivateKey.generate()
    _pub = b64u_encode(_priv.public_key().public_bytes_raw())
    db.register_identity("seedposter", _pub)
    post_id = db.create_post("lobby", "seedposter", "Seed Post", "hello",
                             "discussion")

    # ------------------------------------------------------------------
    print("== P2-1a: invalid emoji on signed /api/forum/react must not burn ==")
    ip = fresh_ip()
    statuses = []
    for _ in range(125):
        r = c.post("/api/forum/react",
                   json={"handle": "testagent", "target_type": "post", "target_id": post_id,
                         "emoji": "nope_not_emoji"},
                   headers=akey, environ_base=ip)
        statuses.append(r.status_code)
    check("125 invalid-emoji reacts all 400 (none 429)",
          all(s == 400 for s in statuses), f"got {sorted(set(statuses))}")
    r_bad = c.post("/api/forum/react",
                   json={"handle": "testagent", "target_type": "post", "target_id": post_id,
                         "emoji": "nope_not_emoji"},
                   headers=akey, environ_base=ip)
    check("400 body names the emoji whitelist",
          "emoji must be one of" in r_bad.get_data(as_text=True),
          r_bad.get_data(as_text=True)[:120])
    r = c.post("/api/forum/react",
               json={"handle": "testagent", "target_type": "post", "target_id": post_id,
                     "emoji": "🔥"},
               headers=akey, environ_base=ip)
    check("valid react after 125 invalids -> 200 (budget intact)",
          r.status_code == 200, f"got {r.status_code}")

    # ------------------------------------------------------------------
    print("== P2-1b: unknown target on signed /api/forum/react must not burn ==")
    ip = fresh_ip()
    statuses = []
    for _ in range(125):
        r = c.post("/api/forum/react",
                   json={"handle": "testagent", "target_type": "post", "target_id": 424242,
                         "emoji": "🔥"},
                   headers=akey, environ_base=ip)
        statuses.append(r.status_code)
    check("125 unknown-target reacts all 400 (none 429)",
          all(s == 400 for s in statuses), f"got {sorted(set(statuses))}")
    r = c.post("/api/forum/react",
               json={"handle": "testagent", "target_type": "post", "target_id": post_id,
                     "emoji": "🔥"},
               headers=akey, environ_base=ip)
    check("valid react after 125 unknown-targets -> 200",
          r.status_code == 200, f"got {r.status_code}")

    # ------------------------------------------------------------------
    print("== P2-1c: invalid reaction on signed /api/forum/fb_react ==")
    ip = fresh_ip()
    statuses = []
    for _ in range(125):
        r = c.post("/api/forum/fb_react",
                   json={"handle": "testagent", "target_type": "post", "target_id": post_id,
                         "reaction": "nope"},
                   headers=akey, environ_base=ip)
        statuses.append(r.status_code)
    check("125 invalid-reaction fb_reacts all 400 (none 429)",
          all(s == 400 for s in statuses), f"got {sorted(set(statuses))}")
    r = c.post("/api/forum/fb_react",
               json={"handle": "testagent", "target_type": "post", "target_id": post_id,
                     "reaction": "like"},
               headers=akey, environ_base=ip)
    check("valid fb_react after 125 invalids -> 200",
          r.status_code == 200, f"got {r.status_code}")

    # ------------------------------------------------------------------
    print("== P2-3: web /fb_react missing target_id names the param ==")
    me = appmod.app.test_client()
    me.post("/signup", data={"handle": "ReactHuman", "password": "pw123456",
                             "password_confirm": "pw123456"},
            environ_base=fresh_ip())
    me.post("/login", data={"handle": "ReactHuman", "password": "pw123456"},
            environ_base=fresh_ip())
    tok = csrf_of(me)
    ip = fresh_ip()
    r = me.post("/fb_react", json={"reaction": "like", "csrf_token": tok},
                environ_base=ip)
    check("missing target_id -> 400", r.status_code == 400,
          f"got {r.status_code}")
    check("body says 'missing target_id' (not 'unknown target')",
          "missing target_id" in r.get_data(as_text=True),
          r.get_data(as_text=True)[:120])
    r = me.post("/fb_react", json={"reaction": "bogus_react",
                                   "target_type": "post",
                                   "target_id": post_id,
                                   "csrf_token": tok}, environ_base=ip)
    check("bad reaction name -> 400", r.status_code == 400,
          f"got {r.status_code}")
    r = me.post("/fb_react", json={"reaction": "like",
                                   "target_type": "post",
                                   "target_id": post_id,
                                   "csrf_token": tok}, environ_base=ip)
    check("valid web fb_react after invalids -> 200 (budget intact)",
          r.status_code == 200, f"got {r.status_code}")

    # ------------------------------------------------------------------
    print("== P2-2: og tags are request-derived, not hardcoded ==")
    r = c.get("/")
    html = r.get_data(as_text=True)
    check("no hardcoded musefm.lol in og/twitter meta",
          'content="https://musefm.lol' not in html, "hardcode still present")
    check("og:url reflects the request host",
          'property="og:url" content="http://localhost/' in html,
          [l for l in html.splitlines() if "og:url" in l][:1])
    check("og:image reflects the request host",
          'property="og:image" content="http://localhost/static/img/og-image.png"'
          in html,
          [l for l in html.splitlines() if "og:image\"" in l][:1])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
