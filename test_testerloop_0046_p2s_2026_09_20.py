#!/usr/bin/env python3
"""
Fixes for the 2026-09-20 00:46 tester-loop P2s.

  P2.3  HTML /vote silently 302'd on bad input (value=999, unknown target)
        -> now 400 / 404 with the error surfaced.
  P2.4  HTML /fb_react silently 302'd on invalid reactions
        -> now 400 with the error surfaced.
  P2.5  link_mentions() escaped BEFORE linkifying, so the URL regex ran
        over entities and pseudo-anchors like
        <a href="https://example.com"> rendered with garbage hrefs
        -> linkify the raw text first, escape after.
  P2.6  Malformed URLs (http://[::1]:bad) were linkified
        -> _valid_url() gate; invalid URLs stay plain text.
  P2.7  429s carried no Retry-After header
        -> Retry-After on check_limit(), the signed-API decorator peek,
           and every human form 429 path.

Run:  .venv/bin/python test_testerloop_0046_p2s_2026_09_20.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-0046-p2s.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def fresh_ip(n):
    return {"REMOTE_ADDR": "10.77.0.%d" % n}


def csrf_of(client):
    html = client.get("/").get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m, "no csrf meta for logged-in client"
    return m.group(1)


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()

    # ---- human session ----
    r = client.post("/signup", data={"handle": "P2Human",
                                     "password": "supersecret1",
                                     "password_confirm": "supersecret1"})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    r = client.post("/login", data={"handle": "P2Human",
                                    "password": "supersecret1"})
    assert r.status_code == 302, r.get_data(as_text=True)[:200]
    tok = csrf_of(client)
    ip = fresh_ip(11)

    # one real post to vote/react on
    r = client.post("/submit", data={"community": "lobby", "title": "p2 t",
                                     "body": "p2 b", "csrf_token": tok},
                    environ_base=ip, follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.get_data(as_text=True)[:200])
    pid = int(r.headers["Location"].rstrip("/").split("/")[-1])

    print("== P2.3: HTML /vote bad input -> 400/404, not silent 302 ==")
    r = client.post("/vote", data={"target_type": "post",
                                   "target_id": str(pid), "value": "999",
                                   "csrf_token": tok}, environ_base=ip)
    check("vote value=999 -> 400", r.status_code == 400, r.status_code)
    check("vote value=999 body names the error",
          b"value must be 1 or -1" in r.get_data(), r.status_code)
    check("vote value=999 stored nothing",
          appmod.db._one("SELECT score FROM posts WHERE id=?",
                         (pid,))["score"] == 0)

    r = client.post("/vote", data={"target_type": "post",
                                   "target_id": "987654321", "value": "1",
                                   "csrf_token": tok}, environ_base=ip)
    check("vote unknown target -> 404", r.status_code == 404, r.status_code)
    check("vote unknown target names it",
          b"unknown target" in r.get_data(), r.status_code)

    r = client.post("/vote", data={"target_type": "post",
                                   "target_id": str(pid), "value": "1",
                                   "csrf_token": tok}, environ_base=ip)
    check("valid vote still 302", r.status_code == 302, r.status_code)
    check("valid vote counted",
          appmod.db._one("SELECT score FROM posts WHERE id=?",
                         (pid,))["score"] == 1)

    r = client.post("/vote", json={"target_type": "post",
                                   "target_id": pid, "value": 999,
                                   "csrf_token": tok}, environ_base=ip)
    check("JSON vote bad value still 400 json",
          r.status_code == 400 and not r.get_json()["ok"], r.status_code)

    print("== P2.4: HTML /fb_react invalid reaction -> 400, not silent 302 ==")
    r = client.post("/fb_react", data={"target_type": "post",
                                       "target_id": str(pid),
                                       "reaction": "lmaooo",
                                       "csrf_token": tok}, environ_base=ip)
    check("react 'lmaooo' -> 400", r.status_code == 400, r.status_code)
    check("react 'lmaooo' body names valid reactions",
          b"reaction must be one of" in r.get_data(), r.status_code)

    import fb_reactions
    check("invalid reaction stored nothing",
          fb_reactions.fb_reaction_counts(appmod.db, "post", pid) == {})

    r = client.post("/fb_react", data={"target_type": "post",
                                       "target_id": str(pid),
                                       "reaction": "",
                                       "csrf_token": tok}, environ_base=ip)
    check("react '' -> 400", r.status_code == 400, r.status_code)

    r = client.post("/fb_react", data={"target_type": "post",
                                       "target_id": str(pid),
                                       "reaction": "like",
                                       "csrf_token": tok}, environ_base=ip)
    check("valid react still 302", r.status_code == 302, r.status_code)
    check("valid react stored",
          fb_reactions.fb_reaction_counts(appmod.db, "post", pid) == {"like": 1})

    print("== P2.5: linkifier no longer mangles pseudo-anchors ==")
    out = appmod.link_mentions('<a href="https://example.com">markdown</a>')
    hrefs = re.findall(r'href="([^"]*)"', out)
    check("pseudo-anchor: no garbage href",
          hrefs and all("&quot;" not in h and "&gt;" not in h for h in hrefs),
          out[:200])
    check("pseudo-anchor: real URL still linkified once",
          out.count('<a href="https://example.com"') == 1, out[:200])
    check("pseudo-anchor: angle brackets stay escaped",
          "&lt;a href=" in out, out[:200])

    print("== P2.6: malformed URLs not linkified; good ones still are ==")
    out = appmod.link_mentions("try http://[::1]:bad ok")
    check("malformed URL not linkified", "<a href" not in out, out[:160])

    out = appmod.link_mentions("see https://example.com/page?a=1&b=2.")
    check("normal URL still linkified",
          '<a href="https://example.com/page?a=1&amp;b=2"' in out, out[:200])
    check("trailing dot not in href",
          ">https://example.com/page?a=1&amp;b=2</a>." in out, out[:200])

    out = appmod.link_mentions("xss <script>alert(1)</script> https://ok.example")
    check("XSS still escaped", "<script>" not in out and
          "&lt;script&gt;" in out, out[:160])
    check("URL beside XSS still linked",
          '<a href="https://ok.example"' in out, out[:200])

    out = appmod.link_mentions("javascript:alert(1) and data:text/plain,hi")
    check("javascript:/data: never linkified", "<a href" not in out, out[:120])

    print("== P2.7: Retry-After on 429s ==")
    with appmod.app.test_request_context("/", environ_base=fresh_ip(12)):
        for _ in range(3):
            assert appmod.check_limit("probe_bucket", 3) is None
        r = appmod.check_limit("probe_bucket", 3)
        check("check_limit 429 carries Retry-After",
              r.status_code == 429 and "Retry-After" in r.headers,
              r.status_code)
        ra = int(r.headers["Retry-After"])
        check("Retry-After is a sane positive int", 1 <= ra <= 3600, ra)

    # burn the HTML /vote bucket (120/hr) on a fixed IP
    vip = fresh_ip(13)
    for _ in range(120):
        client.post("/vote", data={"target_type": "post",
                                   "target_id": str(pid), "value": "1",
                                   "csrf_token": tok}, environ_base=vip)
    r = client.post("/vote", data={"target_type": "post",
                                   "target_id": str(pid), "value": "1",
                                   "csrf_token": tok}, environ_base=vip)
    check("HTML /vote 429 carries Retry-After",
          r.status_code == 429 and "Retry-After" in r.headers,
          (r.status_code, dict(r.headers)))
    check("HTML /vote 429 body still the friendly message",
          b"rate limit hit" in r.get_data(), r.status_code)

    # burn the /submit bucket (5/hr) on a fixed IP
    sip = fresh_ip(14)
    stok_client = appmod.app.test_client()
    stok_client.post("/signup", data={"handle": "P2Poster",
                                      "password": "supersecret1",
                                      "password_confirm": "supersecret1"},
                     environ_base=sip)
    stok_client.post("/login", data={"handle": "P2Poster",
                                     "password": "supersecret1"},
                     environ_base=sip)
    stok = csrf_of(stok_client)
    for i in range(5):
        rr = stok_client.post("/submit",
                              data={"community": "lobby",
                                    "title": "burn %d" % i, "body": "b",
                                    "csrf_token": stok}, environ_base=sip)
        assert rr.status_code == 302, (i, rr.status_code)
    r = stok_client.post("/submit", data={"community": "lobby",
                                          "title": "burn 6", "body": "b",
                                          "csrf_token": stok}, environ_base=sip)
    check("/submit 429 re-renders composer",
          r.status_code == 429 and b"rate limit hit" in r.get_data(),
          r.status_code)
    check("/submit 429 carries Retry-After", "Retry-After" in r.headers,
          dict(r.headers))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
