"""Human->agent DM access tests — temp SQLite DB, no repo state touched.

Covers Anthony's 2026-09-25 order:
- profile Message button (agent profiles only, logged-in, not own, not locked)
- topbar mail icon + unread badge for logged-in humans
- /dm?to=<handle> deep-link staging the 1:1 thread
- human->agent web send (screened, audited, CSRF + rate-limit kept)
- human->human stays blocked; humans can't send into threads they're not in
- typing pings opened for a human's own threads
- owner behavior unchanged
"""
import base64
import json
import os
import secrets
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="dmhuman_")
DBP = os.path.join(TMP, "dm_human_test.db")
REPO = os.path.dirname(os.path.abspath(__file__))
os.environ["TOWNSQUARE_DB"] = DBP
os.environ["AGENT_KEY"] = "testkey123"
os.environ["SESSION_SECRET"] = "testsecret"
os.environ["MUSEFM_MODS"] = "ownerhuman"
os.environ.pop("PORT", None)
sys.path.insert(0, REPO)

import app as A          # noqa: E402  (runs init_db -> dm.ensure_dm_schema)
import dm as DM          # noqa: E402

db = A.db
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + str(extra)) if extra and not cond else ""))


def b64key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


# ---------- setup identities ----------
human = db.register_identity("testhuman", b64key())
human2 = db.register_identity("testhuman2", b64key())
agentA = db.register_identity("testagentA", b64key())
agentB = db.register_identity("testagentB", b64key())
owner = db.register_identity("ownerhuman", b64key())
for h in (human, human2, owner):
    db._exec("UPDATE identities SET password_hash=? WHERE fm_id=?",
             ("x", h["fm_id"]))

H_KEY = "human:" + human["fm_id"]
H2_KEY = "human:" + human2["fm_id"]
A_KEY = "agent:" + agentA["fm_id"]
B_KEY = "agent:" + agentB["fm_id"]


def web_client(fm_id):
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["fm_id"] = fm_id
        s["csrf_token"] = "testcsrf"
    return c


def wpost(c, path, body):
    return c.post(path, data=json.dumps(body),
                  content_type="application/json")


hc = web_client(human["fm_id"])
oc = web_client(owner["fm_id"])
anon = A.app.test_client()

# ---------- 1. profile Message button ----------
r = hc.get("/u/testagentA", follow_redirects=True)
html = r.get_data(as_text=True)
check("profile: Message button on agent profile (logged in)",
      r.status_code == 200 and '/dm?to=testagentA' in html
      and "Message" in html, r.status_code)

r = hc.get("/u/testhuman2", follow_redirects=True)
check("profile: no Message button on human profile",
      '/dm?to=testhuman2' not in r.get_data(as_text=True))

r = hc.get("/u/testhuman", follow_redirects=True)
check("profile: no Message button on own profile",
      '/dm?to=testhuman' not in r.get_data(as_text=True))

r = anon.get("/u/testagentA", follow_redirects=True)
check("profile: no Message button logged out",
      '/dm?to=testagentA' not in r.get_data(as_text=True))

# ---------- 2. /dm?to= deep-link ----------
tkey_ha = DM.thread_key(H_KEY, A_KEY)
r = hc.get("/dm?to=testagentA")
html = r.get_data(as_text=True)
check("/dm?to=agent stages thread",
      r.status_code == 200 and tkey_ha in html
      and 'id="dmComposer"' in html, r.status_code)

r = hc.get("/dm?to=testhuman2")
check("/dm?to=human ignored (no staged thread)",
      r.status_code == 200 and '"thread_key": null' not in r.get_data(as_text=True)
      and "var START = null" in r.get_data(as_text=True))

r = hc.get("/dm?to=nosuchhandle")
check("/dm?to=unknown ignored",
      r.status_code == 200 and "var START = null" in r.get_data(as_text=True))

r = oc.get("/dm?to=testhuman2")
check("owner /dm?to=human allowed (owner may message anyone)",
      DM.thread_key("human:" + owner["fm_id"], H2_KEY) in r.get_data(as_text=True))

r = anon.get("/dm?to=testagentA", follow_redirects=False)
check("/dm?to= logged out redirects to login",
      r.status_code in (301, 302) and "/login" in r.headers.get("Location", ""))

# ---------- 3. human -> agent send ----------
r = wpost(hc, "/api/dm/web/send",
          {"thread_key": tkey_ha,
           "body": "Hey, love your latest track — collab sometime?",
           "csrf_token": "testcsrf"})
d = r.get_json()
check("human->agent send 200", r.status_code == 200 and d["ok"], r.status_code)
check("human->agent disclosure", d.get("disclosure") == DM.DM_DISCLOSURE)
row = db._one("SELECT sender, recipient, thread_key FROM dms WHERE id=?",
              (d["message_id"],))
check("human->agent stored sender/recipient",
      row and row["sender"] == H_KEY and row["recipient"] == A_KEY, row)
audit = db._one("SELECT action FROM dm_audit WHERE message_id=?",
                (d["message_id"],))
check("human->agent audited sent", audit and audit["action"] == "sent", audit)

# thread now lists for the human
r = hc.get("/api/dm/web/threads")
d = r.get_json()
check("human threads include new agent thread",
      any(t["thread_key"] == tkey_ha for t in d["threads"]), d.get("threads"))
r = hc.get("/api/dm/web/thread", query_string={"thread_key": tkey_ha})
d = r.get_json()
check("human reads own agent thread",
      d["ok"] and len(d["messages"]) == 1 and d["messages"][0]["mine"])

# ---------- 4. blocks still hold ----------
r = wpost(hc, "/api/dm/web/send",
          {"thread_key": DM.thread_key(H_KEY, H2_KEY),
           "body": "hey human", "csrf_token": "testcsrf"})
check("human->human send 400",
      r.status_code == 400
      and "only message agents" in r.get_json()["error"].lower(),
      (r.status_code, r.get_json()))

r = wpost(hc, "/api/dm/web/send",
          {"thread_key": DM.thread_key(A_KEY, B_KEY),
           "body": "sneaking in", "csrf_token": "testcsrf"})
check("send into others' thread 403", r.status_code == 403, r.status_code)

r = wpost(hc, "/api/dm/web/send",
          {"thread_key": DM.thread_key(H_KEY, "agent:zzzznonexistent"),
           "body": "hello?", "csrf_token": "testcsrf"})
check("send to unknown agent 404", r.status_code == 404, r.status_code)

r = wpost(hc, "/api/dm/web/send",
          {"thread_key": tkey_ha, "body": "you are a fucking idiot",
           "csrf_token": "testcsrf"})
check("human send still screened",
      r.status_code == 400
      and "professional" in r.get_json()["error"].lower())

r = wpost(hc, "/api/dm/web/send",
          {"thread_key": tkey_ha, "body": "hi", "csrf_token": "wrong"})
check("human send bad csrf 403", r.status_code == 403)

# blocked attempt is audited too
row = db._one("SELECT action, reason FROM dm_audit WHERE sender=? "
              "ORDER BY id DESC LIMIT 1", (H_KEY,))
check("blocked human send audited", row and row["action"] == "blocked", row)

# ---------- 5. typing opened for humans ----------
r = wpost(hc, "/api/dm/web/typing",
          {"thread_key": tkey_ha, "csrf_token": "testcsrf"})
check("human typing on own thread 200",
      r.status_code == 200 and r.get_json()["ok"])
r = wpost(hc, "/api/dm/web/typing",
          {"thread_key": DM.thread_key(A_KEY, B_KEY), "csrf_token": "testcsrf"})
check("human typing on others' thread 403", r.status_code == 403)

# ---------- 6. topbar mail icon + badge ----------
r = hc.get("/")
html = r.get_data(as_text=True)
check("topbar mail icon logged-in", 'class="dm-mail' in html
      and 'href="/dm"' in html)

# agent replies -> unread badge appears
db.dm_send(tkey_ha, A_KEY, H_KEY, "Sure, let's talk tracks!", now=int(time.time()))
r = hc.get("/")
html = r.get_data(as_text=True)
check("topbar mail badge shows unread",
      'dm-mail-badge' in html and ">1<" in html, html.count("dm-mail-badge"))

# reading the thread clears the badge
r = hc.get("/api/dm/web/thread", query_string={"thread_key": tkey_ha})
check("thread fetch marks read", r.get_json()["marked_read"] >= 1)
r = hc.get("/")
check("badge clears after read",
      'dm-mail-badge' not in r.get_data(as_text=True))

r = anon.get("/")
check("no mail icon logged-out", 'class="dm-mail' not in r.get_data(as_text=True))

# ---------- 7. mods can message anyone (2026-09-25, Anthony) ----------
# owner (MUSEFM_MODS=ownerhuman) sees Message on a human profile
r = oc.get("/u/testhuman2", follow_redirects=True)
html = r.get_data(as_text=True)
check("mod: Message button on human profile",
      r.status_code == 200 and '/dm?to=testhuman2' in html, r.status_code)

# non-mod human still gets no Message button on human profiles
r = hc.get("/u/testhuman2", follow_redirects=True)
check("non-mod: no Message button on human profile",
      '/dm?to=testhuman2' not in r.get_data(as_text=True))

# mod deep-link to a human stages the 1:1 thread
tkey_hh = DM.thread_key("human:" + owner["fm_id"], H2_KEY)
r = oc.get("/dm?to=testhuman2")
check("mod /dm?to=human stages thread",
      r.status_code == 200 and tkey_hh in r.get_data(as_text=True),
      r.status_code)

# mod can actually send to a human
csrf = "testcsrf"
r = wpost(oc, "/api/dm/web/send",
          {"thread_key": tkey_hh, "body": "Hey from the owner",
           "csrf_token": csrf})
check("mod send to human 200", r.status_code == 200 and r.get_json()["ok"],
      r.status_code)
row = db._one("SELECT sender, recipient FROM dms WHERE thread_key=?"
              " ORDER BY id DESC LIMIT 1", (tkey_hh,))
check("mod->human stored correctly",
      row and row["sender"] == "human:" + owner["fm_id"]
      and row["recipient"] == H2_KEY, dict(row) if row else None)

# mod deep-link to an agent still works
tkey_ha_owner = DM.thread_key("human:" + owner["fm_id"], A_KEY)
r = oc.get("/dm?to=testagentA")
check("mod /dm?to=agent stages thread",
      r.status_code == 200 and tkey_ha_owner in r.get_data(as_text=True))

# locked profile: mod still gets a Message button, others don't
db.set_privacy(human2["fm_id"], profile="private")
r = oc.get("/u/testhuman2", follow_redirects=True)
check("mod: Message button on locked profile",
      '/dm?to=testhuman2' in r.get_data(as_text=True), r.status_code)
r = hc.get("/u/testhuman2", follow_redirects=True)
html = r.get_data(as_text=True)
check("non-mod: locked profile, no Message button",
      "keeps it private" in html and '/dm?to=testhuman2' not in html)
db.set_privacy(human2["fm_id"], profile="public")

# ---------- 8. humans message their attached (linked) agent ----------
db._exec("INSERT INTO human_muse_links (human_fm_id, muse_fm_id, created_at)"
         " VALUES (?,?,?)",
         (human["fm_id"], agentA["fm_id"], int(time.time())))
r = hc.get("/u/testhuman", follow_redirects=True)
html = r.get_data(as_text=True)
check("linked agent card shows Message button",
      "Linked agent" in html and '/dm?to=testagentA' in html, r.status_code)
r = anon.get("/u/testhuman", follow_redirects=True)
check("linked agent card: no Message button logged out",
      '/dm?to=testagentA' not in r.get_data(as_text=True))

print("\n==== %d passed, %d failed ====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
