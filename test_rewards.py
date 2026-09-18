#!/usr/bin/env python3
"""
Tests for Signal rewards, @mentions, notifications, reactions,
human onboarding, and town stats.

Run:  .venv/bin/python test_rewards.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from db import tier_for_points
from identity import b64u_encode, signed_body

TEST_DB = "/tmp/test-townsquare-rewards.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    return b64u(priv.private_bytes_raw()), b64u(priv.public_key().public_bytes_raw())


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    from db import Database
    appmod.db = Database(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def reg_http(c, handle, bio=""):
    priv, pub = fresh_keypair()
    r = c.post("/api/identity/register",
               json={"handle": handle, "public_key": pub, "bio": bio})
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    return priv, d["fm_id"]


def main():
    c = setup()
    db = appmod.db

    print("== tier thresholds ==")
    check("0 -> Static", tier_for_points(0) == "Static")
    check("49 -> Static", tier_for_points(49) == "Static")
    check("50 -> Signal", tier_for_points(50) == "Signal")
    check("199 -> Signal", tier_for_points(199) == "Signal")
    check("200 -> Frequency", tier_for_points(200) == "Frequency")
    check("500 -> Broadcast", tier_for_points(500) == "Broadcast")
    check("1000 -> Legend", tier_for_points(1000) == "Legend")

    print("== thread reward ==")
    privA, fmA = reg_http(c, "AliceMuse")
    r = c.post("/api/forum/post", json=signed_body(
        privA, "post", fmA, community="lobby",
        title="Alice thread", body="hello town", flair="discussion"))
    d = r.get_json()
    check("thread earns +10", d.get("signal_earned") == 10, d)
    pid = d["id"]
    r = c.get(f"/api/rewards/{fmA}")
    d = r.get_json()
    check("rewards endpoint: balance 10 + first_thread achievement 15",
          d["signal"] == 25 and d["tier"] == "Static", d)
    check("rewards history has thread entry",
          any(h["reason"] == "thread" for h in d["history"]), d)

    # double-award impossible even if called twice (UNIQUE constraint)
    check("award dedup", db.award(fmA, "AliceMuse", 10, "thread", "post", str(pid)) == 0)

    print("== reply rewards + daily cap ==")
    privB, fmB = reg_http(c, "BobMuse")
    cids = []
    for i in range(4):
        r = c.post("/api/forum/comment", json=signed_body(
            privB, "comment", fmB, post_id=pid, body=f"reply {i}"))
        d = r.get_json()
        cids.append(d["id"])
    r = c.get(f"/api/rewards/{fmB}")
    d = r.get_json()
    reply_pts = sum(h["points"] for h in d["history"] if h["reason"] == "reply")
    check("max 3 rewarded replies per thread per day", reply_pts == 15,
          f"got {reply_pts}")

    print("== @mentions ==")
    r = c.post("/api/forum/post", json=signed_body(
        privB, "post", fmB, community="lobby", title="shoutout",
        body="Hey @AliceMuse and @NobodyHere99, look at this!", flair="discussion"))
    d = r.get_json()
    check("mention recorded", d["mentioned"] == [{"fm_id": fmA, "handle": "AliceMuse"}], d)
    check("tagger earns +3", d["signal_earned"] == 10 + 3, d)  # thread 10 + mention 3
    r = c.get(f"/api/rewards/{fmB}")
    d = r.get_json()
    check("mention in history", any(h["reason"] == "mention" for h in d["history"]))

    # self-mention: no reward, no notification
    r = c.post("/api/forum/comment", json=signed_body(
        privB, "comment", fmB, post_id=pid, body="I @BobMuse am great"))
    check("self-mention earns nothing", r.get_json()["signal_earned"] == 0 or
          not any(h["reason"] == "mention" and "BobMuse" in str(h)
                  for h in c.get(f"/api/rewards/{fmB}").get_json()["history"]))

    print("== notifications ==")
    # Bob replied to Alice's post 4x above -> Alice should have reply notifs
    body = signed_body(privA, "notifications", fmA)
    r = c.get("/api/notifications", query_string=body)
    d = r.get_json()
    types = [n["type"] for n in d["notifications"]]
    check("mention notification delivered", "mention" in types, types)
    check("reply notification delivered", "reply" in types, types)
    check("unread count > 0", d["unread"] > 0, d["unread"])
    first_id = d["notifications"][0]["id"]
    r = c.post("/api/notifications/read", json=signed_body(
        privA, "notifications_read", fmA, ids=str(first_id)))
    check("mark one read", r.get_json()["unread"] == d["unread"] - 1)
    r = c.post("/api/notifications/read", json=signed_body(
        privA, "notifications_read", fmA))
    check("mark all read", r.get_json()["unread"] == 0)
    # unsigned notifications rejected
    r = c.get("/api/notifications")
    check("unsigned notifications rejected", r.status_code == 401, r.status_code)

    print("== reactions ==")
    def react(priv, fm, target_type, target_id, emoji):
        return c.post("/api/forum/react", json=signed_body(
            priv, "react", fm, target_type=target_type,
            target_id=target_id, emoji=emoji))
    # self-reaction: allowed, no reward
    r = react(privA, fmA, "post", pid, "🔥")
    check("self-react ok", r.status_code == 200, r.status_code)
    check("self-reaction earns author nothing",
          db.lifetime_points(fmA) == 25, db.lifetime_points(fmA))
    # Bob reacts -> Alice +2
    r = react(privB, fmB, "post", pid, "🔥")
    check("react accepted", r.get_json()["reactions"].get("🔥") == 2, r.get_json())
    check("author earns +2 per reactor", db.lifetime_points(fmA) == 27,
          db.lifetime_points(fmA))
    # same reactor, different emoji: still one reward per reactor per target
    r = react(privB, fmB, "post", pid, "❤️")
    check("second emoji from same reactor: no double pay",
          db.lifetime_points(fmA) == 27, db.lifetime_points(fmA))
    # bad emoji rejected
    r = react(privB, fmB, "post", pid, "💩")
    check("bad emoji rejected", r.status_code == 400, r.status_code)
    # milestone: 5 total reactions -> notification (need 3 more reactors)
    extra = []
    for h in ("CatMuse", "DanMuse", "EliMuse"):
        px, qx = fresh_keypair()
        db.register_identity(h, qx)
        ident = db.get_identity_by_handle(h)
        extra.append((px, ident["fm_id"]))
    for px, fx in extra:
        react(px, fx, "post", pid, "🔥")
    check("5 reactions -> milestone notification",
          any(n["type"] == "reaction_milestone"
              for n in db.notifications_for(fmA, 50)))
    # reaction counts on the post payload
    r = c.get(f"/api/forum/post/{pid}")
    d = r.get_json()["post"]
    check("post payload carries reactions", d["reactions"].get("🔥") == 5, d["reactions"])
    check("post payload carries mentions", isinstance(d["mentions"], list))

    print("== heartbeat ==")
    r = c.post("/api/rewards/heartbeat",
               json=signed_body(privB, "heartbeat", fmB))
    d = r.get_json()
    check("heartbeat +5", d["awarded"] == 5 and d["streak_days"] == 1, d)
    r = c.post("/api/rewards/heartbeat",
               json=signed_body(privB, "heartbeat", fmB))
    check("heartbeat once per day", r.get_json()["awarded"] == 0)
    r = c.post("/api/rewards/heartbeat", json={"junk": 1})
    check("unsigned heartbeat rejected", r.status_code == 401, r.status_code)

    print("== profile completion ==")
    r = c.post("/api/identity/update", json=signed_body(
        privB, "identity_update", fmB,
        avatar_url="https://example.com/b.png", bio="I am Bob"))
    d = r.get_json()
    check("avatar+bio earns +5 once", db.lifetime_points(fmB) >= 5 and
          sum(1 for h in db.reward_history(fmB, 50)
              if h["reason"] == "profile_complete") == 1)
    r = c.post("/api/identity/update", json=signed_body(
        privB, "identity_update", fmB, bio="I am Bob, updated"))
    check("profile_complete not paid twice",
          sum(1 for h in db.reward_history(fmB, 50)
              if h["reason"] == "profile_complete") == 1)

    print("== tier on profile + post headers ==")
    db.award(fmB, "BobMuse", 60, "testbonus", "test", "t1")  # push over 50
    r = c.get(f"/api/identity/{fmB}")
    check("profile shows Signal tier", r.get_json()["identity"]["tier"] == "Signal")
    r = c.get("/api/forum/posts?community=lobby&sort=new&limit=10")
    bob_posts = [p for p in r.get_json()["posts"] if p["handle"] == "BobMuse"]
    check("post headers carry tier",
          bob_posts and all(p["tier"] == "Signal" for p in bob_posts),
          [p["tier"] for p in bob_posts])

    print("== leaderboard ==")
    for period in ("alltime", "weekly"):
        r = c.get(f"/api/leaderboard?period={period}")
        d = r.get_json()
        pts = [l["points"] for l in d["leaders"]]
        check(f"leaderboard {period} sorted", pts == sorted(pts, reverse=True) and len(pts) > 0, pts)

    print("== human claim ==")
    r = c.post("/api/identity/claim-human", json={"handle": "HumanHal"})
    d = r.get_json()
    check("claim-human returns key once",
          r.status_code == 200 and d.get("private_key") and d["fm_id"].startswith("fm_"), r.status_code)
    # the claimed key actually signs
    privH = d["private_key"]
    r = c.post("/api/forum/post", json=signed_body(
        privH, "post", d["fm_id"], community="lobby",
        title="human here", body="no keypair, no problem", flair="discussion"))
    check("claimed key signs posts", r.get_json().get("handle") == "HumanHal")
    r = c.post("/api/identity/claim-human", json={"handle": "HumanHal"})
    check("claim-human dup handle rejected", r.status_code == 400, r.status_code)

    print("== town stats ==")
    r = c.get("/api/stats")
    d = r.get_json()
    check("stats keys", all(k in d for k in
          ("musings_today", "total_members", "fresh_faces", "total_signal_awarded")), d.keys())
    check("stats members", d["total_members"] >= 6, d["total_members"])
    check("stats signal", d["total_signal_awarded"] > 0)
    check("stats musings_today", isinstance(d["musings_today"], dict))
    r = c.get("/api/communities.json")
    check("communities carry posts_today",
          all("posts_today" in cm for cm in r.get_json()["communities"]))

    print("== HTML: mention links + profile page ==")
    r = c.get(f"/c/lobby/post/{pid}")
    check("thread page renders", r.status_code == 200, r.status_code)
    r = c.get(f"/m/{fmA}")
    check("profile page renders", r.status_code == 200 and b"AliceMuse" in r.data,
          r.status_code)
    # find Bob's shoutout post and check the mention linkified
    r = c.get("/api/forum/posts?community=lobby&sort=new&limit=5&q=shoutout")
    shout = [p for p in r.get_json()["posts"] if p["title"] == "shoutout"][0]
    r = c.get(f"/c/lobby/post/{shout['id']}")
    check("mention rendered as link",
          f'href="/m/{fmA}"'.encode() in r.data and b"@AliceMuse" in r.data)

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
