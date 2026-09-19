"""Tester-loop 2026-09-19 (15:35 run): human web-flow error-message quality.

Two P2s found by the human tester against the local scratch instance, both
verified by reading app.py/db.py:

1. /flag JSON with a VALID target but INVALID reason returns 400
   {"ok": false, "error": "bad flag target"}. The message blames the target;
   db.flag_post raises the accurate ValueError("bad reason (spam,
   harassment, nsfw, misinfo, other)") but flag_web()'s except clause swallows
   it into the generic "bad flag target" (app.py ~3907).

2. Rate-limited human FORM posts return raw JSON. /submit POST calls
   check_limit("post", 5) and returns `hit` directly; check_limit always
   returns jsonify({"ok": False, "error": "rate limit hit ..."}), 429 --
   even for form-encoded browser POSTs. A human clicking through the UI gets
   a JSON blob instead of the form re-rendered with a friendly error
   (app.py ~480 / ~1009).

This file is tests ONLY (tester-loop fix policy: no app source changes).
Both tests currently FAIL; they should PASS once the messages are fixed.

Run: python3 test_testerloop_flag_messages_2026_09_19.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-flag-messages-20260919.db"

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


def signup_login(handle, password="supersecret1"):
    human = appmod.app.test_client()
    r = human.post("/signup", data={"handle": handle, "password": password,
                                    "password_confirm": password},
                   environ_base={"REMOTE_ADDR": "10.77.0.11"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    r = human.post("/login", data={"handle": handle, "password": password},
                   environ_base={"REMOTE_ADDR": "10.77.0.12"})
    assert r.status_code == 302, r.get_data(as_text=True)[:200]
    return human


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True


_ipn = [100]


def fresh_test_ip():
    _ipn[0] += 1
    return {"REMOTE_ADDR": "10.77.1.%d" % _ipn[0]}


def make_post(human, n, ip):
    r = human.post("/submit", data={"title": "flag-target %d" % n,
                                    "body": "target body %d" % n,
                                    "community": "lobby",
                                    "csrf_token": csrf_of(human)},
                   environ_base=ip,
                   follow_redirects=False)
    assert r.status_code in (302, 303), "submit failed: %s %s" % (
        r.status_code, r.get_data(as_text=True)[:200])
    # /submit redirects to the new post page: /c/<slug>/post/<pid>
    m = re.search(r"/post/(\d+)", r.headers.get("Location", ""))
    return int(m.group(1)) if m else None


def test_flag_bad_reason_message():
    """JSON /flag on a valid post with an invalid reason should say the
    REASON is bad, not the target."""
    setup()
    human = signup_login("flagmsg_h1")
    pid = make_post(human, 1, fresh_test_ip())
    assert pid, "could not extract post id from /submit redirect"
    csrf = csrf_of(human)
    r = human.post("/flag",
                   json={"target_type": "post", "target_id": pid,
                         "reason": "test flag", "csrf_token": csrf},
                   environ_base={"REMOTE_ADDR": "10.77.0.31"})
    body = r.get_data(as_text=True)
    print("    /flag status=%d body=%s" % (r.status_code, body[:160]))
    check("flag with invalid reason -> 400", r.status_code == 400,
          r.status_code)
    check("flag invalid-reason message mentions 'reason' (not 'target')",
          "reason" in body.lower() and "bad flag target" not in body, body)


def test_submit_rate_limit_renders_html():
    """A rate-limited human FORM /submit should re-render the form page
    (HTML with a friendly error), not dump raw JSON."""
    setup()
    human = signup_login("flagmsg_h2")
    post_ip = fresh_test_ip()
    # burn the 5/hr post budget
    for n in range(5):
        make_post(human, n, post_ip)
    r = human.post("/submit", data={"title": "over the limit",
                                    "body": "should be friendly",
                                    "community": "lobby",
                                    "csrf_token": csrf_of(human)},
                   environ_base=post_ip,
                   follow_redirects=False)
    body = r.get_data(as_text=True)
    print("    6th /submit status=%d content-type=%s body=%s" % (
        r.status_code, r.content_type, body[:160]))
    check("6th /submit -> 429", r.status_code == 429, r.status_code)
    check("429 on form /submit renders HTML, not JSON",
          r.content_type.startswith("text/html"),
          "content-type=%s body=%s" % (r.content_type, body[:120]))


if __name__ == "__main__":
    test_flag_bad_reason_message()
    test_submit_rate_limit_renders_html()
    print("== %d PASS, %d FAIL ==" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)
