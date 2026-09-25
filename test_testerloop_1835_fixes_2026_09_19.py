"""Tester-loop 2026-09-19 (18:35 run): P1 + 6 new P2s, fixed in app.py/db.py.

P1 (human tester): clean() collapsed all whitespace including \\n, so every
multiline post/comment lost its paragraph structure.

P2s (human + adversarial testers):
  #2 /comment/edit JSON echoed the RAW submitted body, not the stored cleaned
     body — the in-place UI showed newlines that vanished on reload.
  #3 Scunthorpe: has_banned() substring matching blocked "snigger".
  #4 Raw Python exception text in API 400s: /vote value "abc", /api/forum/
     fb_react target_id "abc", comment parent_id "abc" ->
     "invalid literal for int() with base 10: 'abc'".
  #5 Floats silently int()-truncated: vote value 1.5 -> 200 recorded as +1.
  #6 No length cap on comment bodies (50KB/1MB -> 200).
     [SUPERSEDED 2026-09-21 22:35 P1: silent truncation lost the tail with a
     200 success. Oversize bodies are now REJECTED with a clear 400; the
     2000-char cap stands via db.clean_comment_body().]
  #7 Rate-limit budget burned by malformed signed posts on /api/forum/post
     (check_limit ran before validation); identity_register was the fixed
     precedent.

Run: python3 test_testerloop_1835_fixes_2026_09_19.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import db as dbmod

TEST_DB = "/tmp/test-townsquare-1835-fixes-20260919.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


def signup_login(handle, ip, password="supersecret1"):
    human = appmod.app.test_client()
    r = human.post("/signup", data={"handle": handle, "password": password,
                                    "password_confirm": password},
                   environ_base={"REMOTE_ADDR": ip})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    r = human.post("/login", data={"handle": handle, "password": password},
                   environ_base={"REMOTE_ADDR": ip})
    assert r.status_code == 302, r.get_data(as_text=True)[:200]
    return human


_ipn = [200]


def fresh_ip():
    _ipn[0] += 1
    return {"REMOTE_ADDR": "10.99.1.%d" % _ipn[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.AGENT_KEY = "test-agent-key-1835"
    appmod.app.config["TESTING"] = True


AGENT_HEADERS = {"X-Agent-Key": "test-agent-key-1835"}


# ------------------------------------------------------------ #3 word filter
def test_scunthorpe():
    check("snigger not blocked", not dbmod.has_banned(
        "he tried not to snigger at the joke"))
    check("standalone slur still blocked", dbmod.has_banned("you nigger"))
    check("fag standalone blocked", dbmod.has_banned("that fag"))
    check("clean prose passes", not dbmod.has_banned("a fine day in the town"))
    # edit-comment route level: "snigger" edit must not 400 on the filter
    human = signup_login("snigtester", "10.99.2.1")
    token = csrf_of(human)
    r = human.post("/submit", data={"title": "filter probe", "body": "x",
                                    "community": "lobby",
                                    "csrf_token": token},
                   environ_base=fresh_ip())
    assert r.status_code in (200, 302), r.get_data(as_text=True)[:200]
    pid = appmod.db._one("SELECT id FROM posts WHERE title='filter probe'")["id"]
    cid = appmod.db.create_comment(pid, None, "snigtester", "first draft")
    r = human.post("/comment/edit",
                   json={"target_type": "comment", "target_id": cid,
                         "body": "he tried not to snigger at the joke",
                         "csrf_token": token},
                   environ_base=fresh_ip())
    body = r.get_json()
    check("edit with 'snigger' accepted", r.status_code == 200 and
          body.get("ok") is True, str(body)[:160])


# ------------------------------------------------------- #2 echo stored body
def test_edit_echoes_stored_body():
    human = signup_login("echotester", "10.99.2.2")
    token = csrf_of(human)
    r = human.post("/submit", data={"title": "echo probe", "body": "x",
                                    "community": "lobby",
                                    "csrf_token": token},
                   environ_base=fresh_ip())
    assert r.status_code in (200, 302)
    pid = appmod.db._one("SELECT id FROM posts WHERE title='echo probe'")["id"]
    cid = appmod.db.create_comment(pid, None, "echotester", "first draft")
    submitted = "para one\n\npara two   with   extra    spaces\nline three"
    r = human.post("/comment/edit",
                   json={"target_type": "comment", "target_id": cid,
                         "body": submitted, "csrf_token": token},
                   environ_base=fresh_ip())
    body = r.get_json()
    stored = appmod.db.get_comment(cid)["body"]
    check("edit JSON ok", r.status_code == 200 and body.get("ok") is True,
          str(body)[:160])
    check("echo matches stored body",
          body.get("body_html") and stored in body["body_html"],
          "echo=%r stored=%r" % (body.get("body_html"), stored)[:200])
    check("newlines preserved in stored body", "\n\n" in stored,
          repr(stored)[:120])
    check("horizontal whitespace collapsed in stored body",
          "   " not in stored, repr(stored)[:120])
    check("no raw-echo of triple spaces", "   " not in
          (body.get("body_html") or ""))


# ------------------------------------------------- #4 clean 400s, #5 floats
def test_int_field_errors():
    api = appmod.app.test_client()
    # /api/forum/vote: non-numeric string -> clean 400
    r = api.post("/api/forum/vote",
                 json={"handle": "floattester", "target_type": "post",
                       "target_id": 1, "value": "abc"},
                 headers=AGENT_HEADERS, environ_base=fresh_ip())
    j = r.get_json()
    check("vote value 'abc' -> 400", r.status_code == 400, str(j)[:120])
    check("vote error is clean (no python internals)",
          "invalid literal" not in (j.get("error") or ""),
          str(j.get("error"))[:120])
    check("vote error names the field",
          "value" in (j.get("error") or ""), str(j.get("error"))[:120])
    # float -> 400, not silent truncation
    r = api.post("/api/forum/vote",
                 json={"handle": "floattester", "target_type": "post",
                       "target_id": 1, "value": 1.5},
                 headers=AGENT_HEADERS, environ_base=fresh_ip())
    check("vote value 1.5 -> 400", r.status_code == 400,
          str(r.get_json())[:120])
    # fb_react target_id float -> 400
    r = api.post("/api/forum/fb_react",
                 json={"handle": "floattester", "target_type": "post",
                       "target_id": 1.5, "reaction": "like"},
                 headers=AGENT_HEADERS, environ_base=fresh_ip())
    check("fb_react target_id 1.5 -> 400", r.status_code == 400,
          str(r.get_json())[:120])
    # fb_react target_id "abc" -> clean 400
    r = api.post("/api/forum/fb_react",
                 json={"handle": "floattester", "target_type": "post",
                       "target_id": "abc", "reaction": "like"},
                 headers=AGENT_HEADERS, environ_base=fresh_ip())
    j = r.get_json()
    check("fb_react target_id 'abc' -> 400 clean", r.status_code == 400 and
          "invalid literal" not in (j.get("error") or ""), str(j)[:120])
    # api comment parent_id "abc" -> clean 400 (needs a real post first)
    pid = appmod.db.create_post("lobby", "floattester", "int probe", "b",
                                "discussion")
    r = api.post("/api/forum/comment",
                 json={"handle": "floattester", "post_id": pid,
                       "parent_id": "abc", "body": "hi"},
                 headers=AGENT_HEADERS, environ_base=fresh_ip())
    j = r.get_json()
    check("comment parent_id 'abc' -> 400 clean", r.status_code == 400 and
          "invalid literal" not in (j.get("error") or "")
          and "NoneType" not in (j.get("error") or ""), str(j)[:120])
    # valid ints still work end to end
    r = api.post("/api/forum/vote",
                 json={"handle": "floattester", "target_type": "post",
                       "target_id": pid, "value": 1},
                 headers=AGENT_HEADERS, environ_base=fresh_ip())
    check("vote value 1 still 200", r.status_code == 200,
          str(r.get_json())[:120])
    r = api.post("/api/forum/vote",
                 json={"handle": "floattester", "target_type": "post",
                       "target_id": str(pid), "value": "1"},
                 headers=AGENT_HEADERS, environ_base=fresh_ip())
    check("vote numeric strings still 200", r.status_code == 200,
          str(r.get_json())[:120])


# ------------------------------------------------------- #6 comment body cap
# P1 2026-09-21: the 18:35 "fix" silently TRUNCATED oversize comment bodies
# (50KB/1MB -> 200, stored head, tail lost). That silent data loss is the
# bug now. Fixed: oversize bodies are REJECTED with a clear 400 and nothing
# is stored; the 2000-char cap itself is preserved via clean_comment_body().
def test_comment_body_cap():
    api = appmod.app.test_client()
    pid = appmod.db.create_post("lobby", "captester", "cap probe", "b",
                                "discussion")
    big = "x" * 50000
    r = api.post("/api/forum/comment",
                 json={"handle": "captester", "post_id": pid, "body": big},
                 headers=AGENT_HEADERS, environ_base=fresh_ip())
    j = r.get_json()
    check("50KB comment -> 400 (rejected, not silently truncated)",
          r.status_code == 400, str(j)[:120])
    check("rejection names the limit",
          "too long" in (j.get("error") or "") and
          "2000" in (j.get("error") or ""), str(j.get("error"))[:160])
    check("oversize comment stored nothing",
          appmod.db._one("SELECT COUNT(*) c FROM comments WHERE post_id=?",
                         (pid,))["c"] == 0)
    # the cap boundary still works: exactly 2000 chars accepted, full length
    edge = "y" * 2000
    r = api.post("/api/forum/comment",
                 json={"handle": "captester", "post_id": pid, "body": edge},
                 headers=AGENT_HEADERS, environ_base=fresh_ip())
    j = r.get_json()
    check("exactly-2000-char comment -> 200", r.status_code == 200,
          str(j)[:120])
    stored = appmod.db.get_comment(j.get("id"))["body"]
    check("2000-char comment stored at full length", len(stored) == 2000,
          "stored len=%d" % len(stored))


# ------------------------------------------- #7 malformed posts don't count
def test_malformed_posts_dont_burn_budget():
    api = appmod.app.test_client()
    ip = fresh_ip()
    # 6 malformed bodies (unknown community) against the 5/hr budget —
    # every one must 400 WITHOUT counting toward the limit.
    codes = []
    for i in range(6):
        r = api.post("/api/forum/post",
                     json={"handle": "budgettester", "community": "nope-%d" % i,
                           "title": "t", "body": "b"},
                     headers=AGENT_HEADERS, environ_base=ip)
        codes.append(r.status_code)
    check("6 malformed posts all 400", all(c == 400 for c in codes),
          str(codes))
    # A valid post right after must still 200 — budget untouched.
    r = api.post("/api/forum/post",
                 json={"handle": "budgettester", "community": "lobby",
                       "title": "budget probe", "body": "b"},
                 headers=AGENT_HEADERS, environ_base=ip)
    j = r.get_json()
    check("valid post after 6 malformed -> 200 (budget intact)",
          r.status_code == 200 and j.get("ok") is True, str(j)[:160])
    # And the budget really is 5/hr: 5 more valid posts -> 4 more 200s then 429.
    codes = []
    for i in range(5):
        r = api.post("/api/forum/post",
                     json={"handle": "budgettester", "community": "lobby",
                           "title": "budget probe %d" % i, "body": "b"},
                     headers=AGENT_HEADERS, environ_base=ip)
        codes.append(r.status_code)
    check("next 4 valid 200 then 429 (limit enforced)",
          codes == [200, 200, 200, 200, 429], str(codes))


if __name__ == "__main__":
    setup()
    test_scunthorpe()
    test_edit_echoes_stored_body()
    test_int_field_errors()
    test_comment_body_cap()
    test_malformed_posts_dont_burn_budget()
    print("== %d PASS, %d FAIL ==" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
