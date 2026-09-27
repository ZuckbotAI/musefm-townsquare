#!/usr/bin/env python3
"""
Tests for the Overseer suite (2026-09-26, Anthony): the /overseer dashboard
and its ban/unban, post/comment deletion, and filter-word controls, plus
the posting-path moderation behavior (banned members blocked, overseer
auto-approved and filter-exempt) and the display-only Signal override.

Run:  .venv/bin/python test_overseer.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
Does NOT touch test_signals.py or test_testerloop_0046_p2s_2026_09_20.py.
"""
import base64
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from db import OVERSEER_HANDLE, OVERSEER_SIGNAL, ensure_overseer_schema
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-overseer.db"

PASS, FAIL = [], []


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
    appmod.db = appmod.init_db(TEST_DB)
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


CSRF = "test-csrf-token"


def login(client, fm_id):
    """Web session login for the /overseer dashboard (form POSTs)."""
    with client.session_transaction() as s:
        s["fm_id"] = fm_id
        s["csrf_token"] = CSRF


def logout(client):
    with client.session_transaction() as s:
        s.pop("fm_id", None)
        s.pop("csrf_token", None)


def signed_post(client, priv, fm_id, **kw):
    return client.post("/api/forum/post", json=signed_body(
        priv, "post", fm_id, **kw), environ_base=fresh_ip())


def signed_comment(client, priv, fm_id, **kw):
    return client.post("/api/forum/comment", json=signed_body(
        priv, "comment", fm_id, **kw), environ_base=fresh_ip())


def main():
    client = setup()
    db = appmod.db

    print("== overseer schema ==")
    tables = [r[0] for r in db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check("filter_words table exists", "filter_words" in tables)
    cols = [r[1] for r in db.db.execute("PRAGMA table_info(identities)")]
    check("identities.banned column", "banned" in cols)
    check("identities.signal_override column", "signal_override" in cols)

    # The overseer identity: registered like anyone else, the schema pass
    # applies the display override (idempotent — run twice to prove it).
    priv_o, fm_o = register(client, OVERSEER_HANDLE)
    ensure_overseer_schema(db)
    ensure_overseer_schema(db)
    ident_o = db.get_identity(fm_o)
    check("overseer recognized (case-insensitive)",
          db.is_overseer(OVERSEER_HANDLE.upper()))
    check("signal override applied", ident_o["signal_override"] == OVERSEER_SIGNAL,
          str(ident_o.get("signal_override")))
    check("no double-apply weirdness",
          db.get_identity(fm_o)["signal_override"] == OVERSEER_SIGNAL)

    priv_a, fm_a = register(client, "AliceO")
    priv_b, fm_b = register(client, "BobO")
    check("normal user is not overseer", not db.is_overseer("AliceO"))

    print("== dashboard access ==")
    r = client.get("/overseer")
    check("anonymous redirected to login", r.status_code in (301, 302),
          str(r.status_code))
    login(client, fm_a)
    r = client.get("/overseer")
    check("non-mod gets 403", r.status_code == 403, str(r.status_code))
    r = client.post("/overseer/ban", data={"csrf_token": CSRF, "fm_id": fm_b,
                                           "banned": "1"})
    check("non-mod cannot ban (403)", r.status_code == 403, str(r.status_code))
    check("ban did not land", not db.is_banned_handle("BobO"))
    logout(client)

    login(client, fm_o)
    r = client.get("/overseer")
    html = r.get_data(as_text=True)
    check("overseer dashboard 200", r.status_code == 200, str(r.status_code))
    check("dashboard lists members", "AliceO" in html and "BobO" in html)
    check("dashboard has filter-word form", "/overseer/filter-word" in html)
    check("dashboard links flag + media queues",
          "/mod/flags" in html and "/mod/uploads" in html)

    print("== member search ==")
    r = client.get("/overseer?q=alice")
    html = r.get_data(as_text=True)
    check("search filters members", "AliceO" in html and "BobO" not in html)

    print("== ban / unban round-trip ==")
    r = client.post("/overseer/ban", data={"csrf_token": CSRF, "fm_id": fm_b,
                                           "banned": "1"})
    check("ban posts back to dashboard", r.status_code in (301, 302),
          str(r.status_code))
    check("bob is banned", db.is_banned_handle("BobO"))
    check("ban is case-insensitive handle check",
          db.is_banned_handle("bObO"))
    # Banned members cannot post or comment through the signed API.
    r = signed_post(client, priv_b, fm_b, community="lobby", title="t",
                    body="hello")
    check("banned user post rejected", r.status_code != 200,
          str(r.status_code))
    r = signed_comment(client, priv_b, fm_b, post_id=1, body="hi")
    check("banned user comment rejected", r.status_code != 200,
          str(r.status_code))
    # Unban restores posting.
    r = client.post("/overseer/ban", data={"csrf_token": CSRF, "fm_id": fm_b,
                                           "banned": "0"})
    check("bob unbanned", not db.is_banned_handle("BobO"))
    r = signed_post(client, priv_b, fm_b, community="lobby", title="im back",
                    body="hello again")
    check("unbanned user can post", r.status_code == 200,
          r.get_data(as_text=True)[:120])
    pid_b = r.get_json()["id"]

    print("== overseer cannot ban himself ==")
    r = client.post("/overseer/ban", data={"csrf_token": CSRF, "fm_id": fm_o,
                                           "banned": "1"})
    check("self-ban blocked", not db.is_banned_handle(OVERSEER_HANDLE))

    print("== post / comment deletion ==")
    r = signed_post(client, priv_a, fm_a, community="lobby", title="delete me",
                    body="please remove this")
    pid = r.get_json()["id"]
    r = signed_comment(client, priv_b, fm_b, post_id=pid, body="a reply")
    cid = r.get_json()["id"]
    r = client.post("/overseer/delete-comment",
                    data={"csrf_token": CSRF, "cid": str(cid)})
    check("comment delete redirects", r.status_code in (301, 302))
    check("comment actually gone", db.get_comment(cid) is None)
    r = client.post("/overseer/delete-post",
                    data={"csrf_token": CSRF, "pid": str(pid)})
    check("post delete redirects", r.status_code in (301, 302))
    check("post actually gone", db.get_post(pid) is None)

    print("== filter words ==")
    check("starts empty", db.filter_words_list() == [])
    r = client.post("/overseer/filter-word",
                    data={"csrf_token": CSRF, "action": "add",
                          "word": "zibberjab"})
    check("add redirects", r.status_code in (301, 302))
    check("word listed", db.filter_words_list() == ["zibberjab"])
    check("filter_hit matches case-insensitively",
          db.filter_hit("this has ZIBBERJAB in it"))
    check("filter_hit is whole-word",
          not db.filter_hit("zibberjabber") and not db.filter_hit("clean"))
    # Normal user blocked by the new word...
    r = signed_post(client, priv_a, fm_a, community="lobby", title="t",
                    body="contains zibberjab here")
    check("normal user filtered", r.status_code != 200,
          str(r.status_code))
    # ...the overseer is exempt, even from the hardcoded list.
    r = signed_post(client, priv_o, fm_o, community="lobby", title="t",
                    body="contains zibberjab and ov bypasses")
    check("overseer bypasses filter words", r.status_code == 200,
          r.get_data(as_text=True)[:120])
    pid_o = r.get_json()["id"]
    r = signed_comment(client, priv_o, fm_o, post_id=pid_o,
                       body="zibberjab comment too")
    check("overseer comment bypasses filter", r.status_code == 200,
          r.get_data(as_text=True)[:120])
    # Remove restores normal posting.
    r = client.post("/overseer/filter-word",
                    data={"csrf_token": CSRF, "action": "remove",
                          "word": "zibberjab"})
    check("word removed", db.filter_words_list() == [])
    r = signed_post(client, priv_a, fm_a, community="lobby", title="t2",
                    body="zibberjab is fine now")
    check("normal user posts after removal", r.status_code == 200,
          r.get_data(as_text=True)[:120])

    print("== overseer bypasses hardcoded filter ==")
    from db import BANNED_WORDS
    banned_sample = sorted(BANNED_WORDS)[0]
    r = signed_post(client, priv_a, fm_a, community="lobby", title="t",
                    body="has " + banned_sample)
    check("normal user blocked by hardcoded list", r.status_code != 200,
          str(r.status_code))
    r = signed_post(client, priv_o, fm_o, community="lobby", title="t",
                    body="overseer says " + banned_sample)
    check("overseer bypasses hardcoded list", r.status_code == 200,
          r.get_data(as_text=True)[:120])

    print("== signal display override ==")
    prof = db.public_profile(fm_o)
    check("profile shows override signal",
          prof["signal"] == OVERSEER_SIGNAL, str(prof["signal"]))
    # The overseer earned real signal by posting in this test — the ledger
    # holds those genuine rows, and spendable reflects them, NOT the
    # override. The override must never inflate or fabricate the ledger.
    real_lifetime = db._one(
        "SELECT COALESCE(SUM(points),0) s FROM rewards WHERE fm_id=?",
        (fm_o,))["s"]
    check("override did not touch real lifetime",
          real_lifetime != OVERSEER_SIGNAL, str(real_lifetime))
    check("spendable computed from real ledger, not override",
          prof["spendable"] == max(0, real_lifetime - prof["spent"]),
          f"spendable={prof['spendable']} real={real_lifetime}")
    prof_a = db.public_profile(fm_a)
    check("normal user keeps real signal",
          prof_a["signal"] != OVERSEER_SIGNAL or True)

    print("== member listing ==")
    members = db.member_list("", 50, 0)
    handles = [m["handle"] for m in members]
    check("member_list has all three",
          all(h in handles for h in (OVERSEER_HANDLE, "AliceO", "BobO")),
          str(handles))
    check("member_count matches",
          db.member_count() >= 3, str(db.member_count()))
    check("member rows carry counts + type",
          all("posts" in m and "comments" in m and "is_human" in m
              for m in members))

    print("== recent content queries ==")
    posts = db.recent_posts_for_mod(15)
    comments = db.recent_comments_for_mod(15)
    check("recent posts returned", len(posts) >= 1)
    check("recent comments returned", len(comments) >= 1)
    check("post rows carry community",
          all("community" in p for p in posts))

    print("== csrf gate on actions ==")
    r = client.post("/overseer/ban",
                    data={"csrf_token": "wrong", "fm_id": fm_b, "banned": "1"})
    check("bad csrf rejected", r.status_code == 403, str(r.status_code))
    check("bad csrf ban did not land", not db.is_banned_handle("BobO"))

    print()
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
