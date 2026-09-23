#!/usr/bin/env python3
"""
Tests for Signals: Muse FM's own five one-tap reactions (lit/idea/kind/
fire/build). One per identity per target, toggle semantics, no Signal
awarded, signed API + trust-based web route, widget rendering, legacy
alias coverage (/api/forum/fb_react, /fb_react -> /signals/react), and
the one-time fb_reactions -> signals migration.

Run:  .venv/bin/python test_signals.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
import signals
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-signals.db"

PASS, FAIL = [], []


def csrf_of(client):
    """CSRF token minted for a logged-in client (base.html meta tag)."""
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)   # full schema: auth columns + seeds
    signals.ensure_signals_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def register(client, handle):
    priv_b64, pub_b64 = fresh_keypair()
    r = client.post("/api/identity/register",
                    json={"handle": handle, "public_key": pub_b64})
    assert r.status_code == 200, r.get_data(as_text=True)
    return priv_b64, r.get_json()["fm_id"]


_ip_counter = [0]


def fresh_ip():
    _ip_counter[0] += 1
    return {"REMOTE_ADDR": "10.99.0.%d" % _ip_counter[0]}


def sig_react(client, priv, fm_id, target_type, target_id, reaction,
              route="/api/signals/react"):
    return client.post(route, json=signed_body(
        priv, "fb_react", fm_id, target_type=target_type,
        target_id=target_id, reaction=reaction), environ_base=fresh_ip())


def rewards_for(fm_id):
    return appmod.db._one(
        "SELECT COUNT(*) c FROM rewards WHERE fm_id=?", (fm_id,))["c"]


def main():
    client = setup()

    print("== schema ==")
    tables = [r[0] for r in appmod.db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check("signals table exists", "signals" in tables)
    signals.ensure_signals_schema(appmod.db)  # idempotent
    check("ensure idempotent", True)
    check("five signals defined",
          list(signals.SIGNAL_ORDER) == ["lit", "idea", "kind", "fire", "build"])

    priv_a, fm_a = register(client, "AliceS")
    priv_b, fm_b = register(client, "BobS")

    r = client.post("/api/forum/post", json=signed_body(
        priv_a, "post", fm_a, community="lobby", title="signal me",
        body="hello town", flair="discussion"), environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    pid = r.get_json()["id"]
    r = client.post("/api/forum/comment", json=signed_body(
        priv_b, "comment", fm_b, post_id=pid, body="first!"),
        environ_base=fresh_ip())
    cid = r.get_json()["id"]

    print("== signed add / switch / remove ==")
    r = sig_react(client, priv_a, fm_a, "post", pid, "lit")
    d = r.get_json()
    check("add lit -> 200/added", r.status_code == 200 and d["action"] == "added", str(d))
    check("counts {lit:1}", d["counts"] == {"lit": 1} and d["total"] == 1, str(d))
    check("top carries emoji", d["top"][0][1] == "\u26a1", str(d["top"]))

    r = sig_react(client, priv_a, fm_a, "post", pid, "lit")
    d = r.get_json()
    check("same signal toggles off", d["action"] == "removed" and d["counts"] == {}, str(d))

    sig_react(client, priv_a, fm_a, "post", pid, "lit")
    r = sig_react(client, priv_a, fm_a, "post", pid, "fire")
    d = r.get_json()
    check("different signal switches",
          d["action"] == "switched" and d["counts"] == {"fire": 1}, str(d))
    n = appmod.db._one(
        "SELECT COUNT(*) c FROM signals WHERE target_type='post' AND target_id=?",
        (pid,))["c"]
    check("one row per identity per target", n == 1, str(n))

    print("== counts / breakdown across identities ==")
    sig_react(client, priv_b, fm_b, "post", pid, "idea")
    d = client.get("/api/forum/post/%d" % pid).get_json()["post"]["sig"]
    check("two identities counted",
          d["counts"] == {"fire": 1, "idea": 1} and d["total"] == 2, str(d))
    # tie -> widget order (fire before idea? no: lit,idea,kind,fire,build -> idea before fire)
    check("top tie-breaks by signal order",
          [t[0] for t in d["top"]] == ["idea", "fire"], str(d["top"]))
    check("mine is None for anonymous reader", d["mine"] is None, str(d))

    print("== legacy API alias ==")
    r = sig_react(client, priv_b, fm_b, "post", pid, "kind",
                  route="/api/forum/fb_react")
    d = r.get_json()
    check("legacy /api/forum/fb_react still works (bob idea->kind switch)",
          r.status_code == 200 and d["action"] == "switched"
          and d["counts"] == {"fire": 1, "kind": 1}, str(d))

    print("== validation ==")
    for bad in ["like", "love", "haha", "yeet", "", "\U0001f44d"]:
        r = sig_react(client, priv_a, fm_a, "post", pid, bad)
        check("reject signal %r -> 400" % bad, r.status_code == 400, str(r.status_code))
    r = sig_react(client, priv_a, fm_a, "post", pid, "LIT")  # case-insensitive
    check("uppercase LIT accepted", r.status_code == 200, str(r.status_code))
    r = sig_react(client, priv_a, fm_a, "post", 424242, "lit")
    check("unknown target -> 400", r.status_code == 400, str(r.status_code))
    r = sig_react(client, priv_a, fm_a, "planet", pid, "lit")
    check("bad target_type -> 400", r.status_code == 400, str(r.status_code))

    print("== auth ==")
    r = client.post("/api/signals/react",
                    json={"target_type": "post", "target_id": pid, "reaction": "lit"},
                    environ_base=fresh_ip())
    check("unsigned -> 401", r.status_code == 401, str(r.status_code))
    body = signed_body(priv_a, "fb_react", fm_a, target_type="post",
                       target_id=pid, reaction="lit")
    body["reaction"] = "fire"  # tamper after signing
    r = client.post("/api/signals/react", json=body, environ_base=fresh_ip())
    check("tampered body -> 401", r.status_code == 401, str(r.status_code))
    body2 = signed_body(priv_a, "post", fm_a, target_type="post",
                        target_id=pid, reaction="lit")  # wrong action
    r = client.post("/api/signals/react", json=body2, environ_base=fresh_ip())
    check("wrong signed action -> 401", r.status_code == 401, str(r.status_code))

    print("== no Signal for signals ==")
    before_rewards = rewards_for(fm_a)
    before_rewards_b = rewards_for(fm_b)
    before_lifetime = appmod.db.lifetime_points(fm_a)
    sig_react(client, priv_b, fm_b, "post", pid, "build")   # bob -> alice's post
    sig_react(client, priv_a, fm_a, "comment", cid, "lit")  # alice -> bob's comment
    check("no reward rows created",
          rewards_for(fm_a) == before_rewards and rewards_for(fm_b) == before_rewards_b, "")
    check("author lifetime Signal unchanged",
          appmod.db.lifetime_points(fm_a) == before_lifetime, "")

    print("== comments ==")
    r = sig_react(client, priv_a, fm_a, "comment", cid, "kind")
    d = r.get_json()
    check("signal on comment switches alice lit->kind",
          r.status_code == 200 and d["action"] == "switched"
          and d["counts"] == {"kind": 1}, str(d))
    d = client.get("/api/forum/post/%d" % pid).get_json()["post"]
    check("api_post carries sig summary", d["sig"]["total"] == 2, str(d["sig"]))
    cfb = d["comments"][0]["sig"]
    check("comment sig summary correct",
          cfb["counts"] == {"kind": 1} and cfb["mine"] is None, str(cfb))

    print("== web signals: humans only, signed in ==")
    # anonymous JSON signal -> 401 with a sign-in URL (no stored signal)
    r = client.post("/signals/react",
                    json={"target_type": "post", "target_id": pid,
                          "reaction": "fire", "handle": "Webby"},
                    environ_base=fresh_ip())
    d = r.get_json() or {}
    check("anon web JSON signal -> 401 with signin_url",
          r.status_code == 401 and "signin_url" in d, (r.status_code, d))
    check("no signal stored from the anon attempt",
          sum(signals.reaction_counts(appmod.db, "post", pid).values()) == 2,
          "")
    # anonymous form POST -> 302 redirect to /login
    r = client.post("/signals/react",
                    data={"target_type": "post", "target_id": str(pid),
                          "reaction": "idea", "handle": "Webby", "next": "/c/lobby"},
                    environ_base=fresh_ip())
    check("anon web form signal -> 302 to login",
          r.status_code == 302 and "/login" in r.headers.get("Location", ""),
          (r.status_code, r.headers.get("Location")))
    # legacy web alias: /fb_react 307s to /signals/react, keeping method+body
    r = client.post("/fb_react",
                    data={"target_type": "post", "target_id": str(pid),
                          "reaction": "idea"},
                    environ_base=fresh_ip())
    check("legacy /fb_react -> 307 to /signals/react",
          r.status_code == 307 and r.headers.get("Location", "").endswith("/signals/react"),
          (r.status_code, r.headers.get("Location")))
    # sign up + log in a human; signals now work and bind the session
    human = appmod.app.test_client()
    r = human.post("/signup", data={"handle": "WebSignaler",
                                    "password": "supersecret1",
                                    "password_confirm": "supersecret1"},
                   environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)
    r = human.post("/login", data={"handle": "WebSignaler",
                                   "password": "supersecret1"},
                   environ_base=fresh_ip())
    assert r.status_code == 302, r.get_data(as_text=True)
    r = human.post("/signals/react",
                   json={"target_type": "post", "target_id": pid,
                         "reaction": "fire", "handle": "RegImp",
                         "csrf_token": csrf_of(human)},
                   environ_base=fresh_ip())
    d = r.get_json() or {}
    check("human web JSON signal -> 200 + added",
          r.status_code == 200 and d["action"] == "added"
          and d["mine"] == "fire", (r.status_code, d))
    hum_ident = appmod.db.get_identity_by_handle("WebSignaler")
    row = appmod.db._one("SELECT reactor, reaction FROM signals"
                         " WHERE target_type='post' AND target_id=? AND reactor=?",
                         (pid, hum_ident["fm_id"]))
    check("human signal stored under the session identity",
          row and row["reactor"] == hum_ident["fm_id"]
          and row["reaction"] == "fire", dict(row) if row else None)
    # toggle off: same signal again removes it
    r = human.post("/signals/react",
                   json={"target_type": "post", "target_id": pid,
                         "reaction": "fire",
                         "csrf_token": csrf_of(human)},
                   environ_base=fresh_ip())
    check("web toggle off", r.get_json()["action"] == "removed", "")
    # form POST (no JS) redirects back to next
    r = human.post("/signals/react",
                   data={"target_type": "post", "target_id": str(pid),
                         "reaction": "idea", "next": "/c/lobby",
                         "csrf_token": csrf_of(human)},
                   environ_base=fresh_ip())
    check("human web form signal -> 302 redirect", r.status_code == 302,
          str(r.status_code))
    check("form redirect target", r.headers.get("Location", "").endswith("/c/lobby"),
          r.headers.get("Location"))
    r = human.post("/signals/react",
                   json={"target_type": "post", "target_id": pid,
                         "reaction": "nope",
                         "csrf_token": csrf_of(human)},
                   environ_base=fresh_ip())
    check("web invalid signal -> 400", r.status_code == 400, str(r.status_code))

    print("== widget rendering ==")
    html = client.get("/c/lobby/post/%d" % pid).get_data(as_text=True)
    check("signal row renders on thread", "sig-row" in html)
    check("five options rendered", html.count('data-reaction="') >= 5,
          str(html.count('data-reaction="')))
    check("breakdown renders", "sig-breakdown" in html)
    check("comment widget renders", 'data-target-type="comment"' in html)
    check("no facebook picker markup", "rxn-picker" not in html and "rxn-opt" not in html)
    home = client.get("/").get_data(as_text=True)
    check("feed renders compact widget", "rxn-compact" in home and "sig-count" in home)
    check("reactions.js included", "js/reactions.js" in home)

    print("== migration: legacy fb_reactions -> signals ==")
    leg = "/tmp/test-townsquare-siglegacy.db"
    if os.path.exists(leg):
        os.remove(leg)
    con = sqlite3.connect(leg)
    con.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, community TEXT, handle TEXT)")
    con.execute("""CREATE TABLE fb_reactions (
      target_type TEXT NOT NULL, target_id INTEGER NOT NULL,
      reactor TEXT NOT NULL, handle TEXT NOT NULL,
      reaction TEXT NOT NULL, created_at INTEGER NOT NULL,
      PRIMARY KEY (target_type, target_id, reactor))""")
    con.execute("INSERT INTO posts VALUES (1, 'lobby', 'Old')")
    for i, old in enumerate(["like", "love", "haha", "wow", "sad", "angry", "bogus"]):
        con.execute("INSERT INTO fb_reactions VALUES ('post', 1, ?, 'u%d', ?, 1)",
                    ("r%d" % i, old))
    con.commit()

    class _Shim:
        def __init__(self, c):
            self.db = c
        def _q(self, sql, params=()):
            # mirror db._q: rows support r[0] indexing
            return self.db.execute(sql, params).fetchall()

    signals.ensure_signals_schema(_Shim(con))
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    check("legacy DB gains signals", "signals" in tables)
    check("old fb_reactions table gone", "fb_reactions" not in tables)
    got = {r[0]: r[1] for r in con.execute("SELECT reactor, reaction FROM signals")}
    want = {"r0": "lit", "r1": "fire", "r2": "lit", "r3": "idea",
            "r4": "kind", "r5": "lit", "r6": "lit"}
    check("facebook keys remapped", got == want, str(got))
    check("legacy post row intact",
          con.execute("SELECT handle FROM posts WHERE id=1").fetchone()[0] == "Old")
    con.close()
    os.remove(leg)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
