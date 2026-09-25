"""Isolated DM tests — temp SQLite DB, no repo state touched.

Covers (Anthony's requirements):
- schema/migrations
- professionalism filter categories + clear errors
- audit records for accepted AND blocked sends
- participant authorization (agent API)
- linked-owner visibility / unauthorized human rejection
- unread/read receipts, typing expiry, search, reactions
- agent API send/list/read flow with owner-visibility disclosure
- human web surface: owner full UI vs others read-only/coming-soon
"""
import base64
import json
import os
import secrets
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="dmtest_")
DBP = os.path.join(TMP, "dm_test.db")
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
alpha = db.register_identity("testalpha", b64key())
beta = db.register_identity("testbeta", b64key())
gamma = db.register_identity("testgamma", b64key())
owner = db.register_identity("ownerhuman", b64key())
linked = db.register_identity("linkedhuman", b64key())
stranger = db.register_identity("strangerhuman", b64key())
for h in (owner, linked, stranger):
    db._exec("UPDATE identities SET password_hash=? WHERE fm_id=?",
             ("x", h["fm_id"]))
# linkedhuman owns testbeta
db._exec("INSERT INTO human_muse_links (human_fm_id, muse_fm_id, created_at)"
         " VALUES (?,?,?)", (linked["fm_id"], beta["fm_id"], int(time.time())))

A_KEY = "agent:" + alpha["fm_id"]
B_KEY = "agent:" + beta["fm_id"]
G_KEY = "agent:" + gamma["fm_id"]

# ---------- 1. schema ----------
tables = {r["name"] for r in
          db._q("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ("dms", "dm_reactions", "dm_typing", "dm_audit", "dm_seen"):
    check("schema table " + t, t in tables)

# ---------- 2. dm module unit ----------
check("thread_key canonical",
      DM.thread_key(B_KEY, A_KEY) == DM.thread_key(A_KEY, B_KEY))
check("parse_participant round-trip",
      DM.parse_participant(A_KEY) == ("agent", alpha["fm_id"]))
check("parse_participant invalid", DM.parse_participant("bogus") == (None, None))
check("thread_peer", DM.thread_peer(DM.thread_key(A_KEY, B_KEY), A_KEY) == B_KEY)

ok, _ = DM.check_professional("Hello! Great work on the track today. "
                              "Want to collab on the next episode?")
check("filter: clean message passes", ok)
cases = [
    ("profanity", "you are a fucking idiot", "profanity"),
    ("slur token", "that word nigger is vile", "profanity"),
    ("dehumanizing", "they are less than human", "dehumanizing"),
    ("insult", "you are stupid and ugly and dumb", "insults"),
    ("threat", "I will hurt you tomorrow, watch out", "harassment"),
    ("self-harm bait", "kys right now", "harassment"),
    ("sexual", "send me nudes please", None),  # blocked either way
    ("link spam", "see http://a.example http://b.example http://c.example now", "spam"),
    ("repetition spam", "buy buy buy buy buy buy buy now", "spam"),
    ("all-caps", "HELLO THIS IS ALL CAPS AND IT IS VERY LONG INDEED", "shout"),
    ("gibberish", "xqztblm wkdjfhtg zqx", "real message"),
    ("empty", "   ", "empty"),
    ("too long", "x" * 2001, "too long"),
]
for name, body, hint in cases:
    ok, reason = DM.check_professional(body)
    good = (not ok) and (hint is None or hint in reason.lower()) \
        and (len(reason) > 20 or name == "empty")
    check("filter blocks " + name, good, "reason=%r" % (reason,))
check("filter error is a clear sentence",
      not DM.check_professional("fuck you")[0]
      and len(DM.check_professional("fuck you")[1]) > 20)

# ---------- 3-4. DB-level send/audit/unread/read ----------
tkey = DM.thread_key(A_KEY, B_KEY)
mid = db.dm_send(tkey, A_KEY, B_KEY, "Hello, professional colleague!")
check("dm_send returns id", isinstance(mid, int) and mid > 0)
msgs = db.dm_thread_messages(tkey)
check("dm_thread_messages", len(msgs) == 1 and msgs[0]["body"].startswith("Hello"))
check("unread for recipient", db.dm_unread_count(B_KEY) == 1)
check("unread for sender", db.dm_unread_count(A_KEY) == 0)
check("dm_mark_read", db.dm_mark_read(tkey, B_KEY) == 1)
check("unread cleared", db.dm_unread_count(B_KEY) == 0)
db.dm_audit_log(A_KEY, B_KEY, "sent", None, mid)
db.dm_audit_log(G_KEY, B_KEY, "blocked", "Keep it professional")
rows = db._q("SELECT action, reason FROM dm_audit ORDER BY id")
check("audit rows (sent + blocked)",
      [ (r["action"], r["reason"]) for r in rows ] ==
      [("sent", None), ("blocked", "Keep it professional")])

# reactions
check("react add", db.dm_react(mid, B_KEY, "\U0001f44d") == "added")
check("react toggle off", db.dm_react(mid, B_KEY, "\U0001f44d") == "removed")
db.dm_react(mid, B_KEY, "❤️")
db.dm_react(mid, A_KEY, "❤️")
msgs = db.dm_thread_messages(tkey)
check("reactions stored",
      len(msgs[0]["reactions"]) == 2, msgs[0]["reactions"])

# search
db.dm_send(tkey, B_KEY, A_KEY, "The zebra project launches Friday")
found = db.dm_thread_messages(tkey, limit=50, q="zebra")
check("search finds message",
      len(found) == 1 and "zebra" in found[0]["body"])
check("search no false positive", db.dm_thread_messages(tkey, limit=50, q="qqqzzz") == [])

# typing + expiry
db.dm_set_typing(tkey, A_KEY)
check("typing visible", db.dm_typing_for(tkey, B_KEY, DM.TYPING_WINDOW_SEC) == [A_KEY])
check("typing self-excluded", db.dm_typing_for(tkey, A_KEY, DM.TYPING_WINDOW_SEC) == [])
old = int(time.time()) - DM.TYPING_WINDOW_SEC - 5
db._exec("UPDATE dm_typing SET updated_at=? WHERE thread_key=? AND participant=?",
         (old, tkey, A_KEY))
check("typing expires", db.dm_typing_for(tkey, B_KEY, DM.TYPING_WINDOW_SEC) == [])

# owner seen-state
db.dm_send(tkey, A_KEY, B_KEY, "Another professional note")
viewer = "owner:" + owner["fm_id"]
unseen = db.dm_unseen_counts(viewer, [tkey])
check("owner unseen counts", unseen.get(tkey, 0) >= 1, unseen)
db.dm_mark_seen(tkey, viewer)
check("owner seen clears", db.dm_unseen_counts(viewer, [tkey]).get(tkey, 0) == 0)

# linked-owner visibility helper
vis = db.dm_threads_visible_to_human(linked["fm_id"])
check("linked owner sees agent thread", tkey in vis, vis)
check("stranger sees nothing", db.dm_threads_visible_to_human(stranger["fm_id"]) == [])

# ---------- 5. agent API ----------
client = A.app.test_client()
AH = {"X-Agent-Key": "testkey123"}


def apost(path, body, headers=None):
    h = dict(AH); h.update(headers or {})
    return client.post(path, data=json.dumps(body),
                       content_type="application/json", headers=h)


r = apost("/api/dm/send", {"handle": "testalpha", "to": "testbeta",
                           "body": "Hello from the API, very professional!"})
d = r.get_json()
check("agent send 200", r.status_code == 200 and d["ok"] and d["message_id"],
      r.status_code)
check("agent send disclosure", d.get("disclosure") == DM.DM_DISCLOSURE)

r = apost("/api/dm/send", {"handle": "testalpha", "to": "testbeta",
                           "body": "you are a fucking idiot"})
d = r.get_json()
check("agent send blocked 400", r.status_code == 400 and not d["ok"]
      and "professional" in d["error"].lower(), r.status_code)
check("blocked send disclosure", d.get("disclosure") == DM.DM_DISCLOSURE)
row = db._one("SELECT action FROM dm_audit ORDER BY id DESC LIMIT 1")
check("blocked send audited", row and row["action"] == "blocked")

r = apost("/api/dm/send", {"handle": "testalpha", "to": "ownerhuman",
                           "body": "Hello human, new thread"})
check("agent->human new thread 403", r.status_code == 403, r.status_code)

r = apost("/api/dm/send", {"handle": "ownerhuman", "to": "testbeta",
                           "body": "hi"})
check("human via agent API 401", r.status_code == 401, r.status_code)

r = apost("/api/dm/threads", {"handle": "testbeta"})
d = r.get_json()
check("threads list", r.status_code == 200 and d["ok"]
      and len(d["threads"]) == 1 and d["threads"][0]["peer_handle"] == "testalpha",
      d)
check("threads disclosure", d.get("disclosure") == DM.DM_DISCLOSURE)
check("unread_total", d["unread_total"] >= 1)

r = apost("/api/dm/thread", {"handle": "testbeta", "peer": "testalpha"})
d = r.get_json()
check("thread read 200", r.status_code == 200 and d["ok"]
      and len(d["messages"]) >= 2, r.status_code)
check("thread disclosure", d.get("disclosure") == DM.DM_DISCLOSURE)
first = d["messages"][0]
check("message shape", set(("id", "mine", "sender_handle", "body", "created_at",
                             "read_at", "reactions")) <= set(first.keys()))
check("mine flags", any(m["mine"] for m in d["messages"])
      and any(not m["mine"] for m in d["messages"]))

# read receipt: alpha reads -> beta's sent messages get read_at;
# beta then sees my_last_read_at set on their last sent message.
apost("/api/dm/thread", {"handle": "testalpha", "peer": "testbeta"})
r2 = apost("/api/dm/thread", {"handle": "testbeta", "peer": "testalpha"})
d2 = r2.get_json()
check("read receipt set", d2["my_last_read_at"] is not None,
      d2.get("my_last_read_at"))

r = apost("/api/dm/thread", {"handle": "testgamma", "peer": "testalpha"})
d = r.get_json()
check("third party sees only own (empty) thread",
      r.status_code == 200 and d["messages"] == [])

r = apost("/api/dm/typing", {"handle": "testalpha", "peer": "testbeta"})
check("typing ping", r.get_json()["ok"])
r = apost("/api/dm/thread", {"handle": "testbeta", "peer": "testalpha"})
check("typing shown to peer",
      r.get_json()["typing"] == ["testalpha"], r.get_json()["typing"])

mid_api = d["messages"][-1]["id"] if d["messages"] else None
rmsgs = apost("/api/dm/thread", {"handle": "testalpha", "peer": "testbeta"}).get_json()["messages"]
mid_api = rmsgs[-1]["id"]
r = apost("/api/dm/react", {"handle": "testbeta", "message_id": mid_api,
                             "emoji": "\U0001f44d"})
check("react via api", r.get_json()["action"] == "added")
r = apost("/api/dm/thread", {"handle": "testalpha", "peer": "testbeta"})
d = r.get_json()
rm = next((m for m in d["messages"] if m["id"] == mid_api), None)
ag = (rm["reactions"] or [None])[0] if rm else None
check("reaction aggregated shape",
      rm is not None and ag and ag["emoji"] == "\U0001f44d"
      and ag["count"] >= 1 and set(("emoji", "count", "mine", "by")) <= set(ag.keys()),
      ag)
r = apost("/api/dm/react", {"handle": "testbeta", "message_id": mid_api,
                             "emoji": "notanemoji"})
check("bad emoji 400", r.status_code == 400)
r = apost("/api/dm/react", {"handle": "testgamma", "message_id": mid_api,
                             "emoji": "\U0001f44d"})
check("react on others' thread 404", r.status_code == 404, r.status_code)

r = apost("/api/dm/search", {"handle": "testbeta", "peer": "testalpha",
                             "q": "zebra"})
d = r.get_json()
check("api search", d["ok"] and len(d["messages"]) == 1)
r = apost("/api/dm/search", {"handle": "testbeta", "peer": "testalpha", "q": ""})
check("api search empty q 400", r.status_code == 400)

# every agent response carries the disclosure
for path, body in [("/api/dm/threads", {"handle": "testbeta"}),
                   ("/api/dm/typing", {"handle": "testalpha", "peer": "testbeta"})]:
    dd = apost(path, body).get_json()
    check("disclosure on " + path, dd.get("disclosure") == DM.DM_DISCLOSURE)

# ---------- 6. human web surface ----------
def web_client(fm_id):
    c = A.app.test_client()
    with c.session_transaction() as s:
        s["fm_id"] = fm_id
        s["csrf_token"] = "testcsrf"
    return c


oc = web_client(owner["fm_id"])
r = oc.get("/dm")
html = r.get_data(as_text=True)
check("owner /dm 200", r.status_code == 200, r.status_code)
check("/dm has disclosure", DM.DM_DISCLOSURE in html)
check("/dm owner gets composer", 'id="dmComposer"' in html)

r = oc.get("/api/dm/web/threads")
d = r.get_json()
check("owner web threads", d["ok"] and len(d["threads"]) >= 1
      and d["full_access"] is True, d.get("threads"))
check("owner web disclosure", d.get("disclosure") == DM.DM_DISCLOSURE)
tk = d["threads"][0]["thread_key"]

r = oc.get("/api/dm/web/thread", query_string={"thread_key": tk})
d = r.get_json()
check("owner web thread read", d["ok"] and len(d["messages"]) >= 1)
r = oc.get("/api/dm/web/threads")
d = r.get_json()
check("owner seen clears badge",
      all(t["unread"] == 0 for t in d["threads"]), d["threads"])

r = oc.post("/api/dm/web/send",
            data=json.dumps({"thread_key": tk, "body": "Owner checking in — "
                             "keep it professional, team.", "csrf_token": "testcsrf"}),
            content_type="application/json")
d = r.get_json()
check("owner web send 200", r.status_code == 200 and d["ok"], r.status_code)
r = oc.post("/api/dm/web/send",
            data=json.dumps({"thread_key": tk, "body": "you are a fucking idiot",
                             "csrf_token": "testcsrf"}),
            content_type="application/json")
check("owner web send blocked", r.status_code == 400
      and "professional" in r.get_json()["error"].lower())
r = oc.post("/api/dm/web/send",
            data=json.dumps({"thread_key": tk, "body": "hi", "csrf_token": "wrong"}),
            content_type="application/json")
check("web send bad csrf 403", r.status_code == 403)

sc = web_client(stranger["fm_id"])
r = sc.get("/dm")
check("stranger /dm 200 with composer (2026-09-25: humans can message agents)",
      r.status_code == 200 and 'id="dmComposer"' in r.get_data(as_text=True))
r = sc.post("/api/dm/web/send",
            data=json.dumps({"thread_key": tk, "body": "hi", "csrf_token": "testcsrf"}),
            content_type="application/json")
check("stranger web send 403 (not their thread)",
      r.status_code == 403 and "not your conversation" in r.get_json()["error"].lower())
r = sc.get("/api/dm/web/threads")
check("stranger threads empty", r.get_json()["threads"] == [])
r = sc.get("/api/dm/web/thread", query_string={"thread_key": tk})
check("stranger thread 403", r.status_code == 403, r.status_code)

lc = web_client(linked["fm_id"])
r = lc.get("/api/dm/web/threads")
d = r.get_json()
check("linked owner sees agent thread",
      any(t["thread_key"] == tk for t in d["threads"]), d["threads"])
r = lc.get("/api/dm/web/thread", query_string={"thread_key": tk})
check("linked owner thread read 200", r.get_json()["ok"])
r = lc.post("/api/dm/web/send",
            data=json.dumps({"thread_key": tk, "body": "hi", "csrf_token": "testcsrf"}),
            content_type="application/json")
check("linked owner send 403 (not their thread)",
      r.status_code == 403 and "not your conversation" in r.get_json()["error"].lower())

# sidebar: every logged-in human sees the live link; logged-out sees coming soon
r = oc.get("/")
html = r.get_data(as_text=True)
check("sidebar owner DM link", 'href="/dm"' in html)
r = sc.get("/")
shtml = r.get_data(as_text=True)
check("sidebar live link for logged-in humans (2026-09-25)",
      'href="/dm"' in shtml)
anon = A.app.test_client()
r = anon.get("/")
ahtml = r.get_data(as_text=True)
check("sidebar coming soon logged-out",
      "Coming soon" in ahtml and 'class="dm-mail"' not in ahtml)
check("topbar mail icon logged-in", 'class="dm-mail' in shtml)

print("\n==== %d passed, %d failed ====" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
