"""Route-level DM notification test (2026-09-27): exercises the real Flask
routes with faked login sessions.

- human web send -> bell notification row for the agent recipient
- agent reply -> bell notification for the human
- human opens thread -> bell notification clears
- owner topbar badge: lights on new thread activity, clears after owner
  opens the thread (seen-state, id-based: same-second safe)
"""
import base64, json, os, secrets, sys, tempfile

TMP = tempfile.mkdtemp(prefix="dmroute_")
DBP = os.path.join(TMP, "dm_route.db")
REPO = os.path.dirname(os.path.abspath(__file__))
os.environ["TOWNSQUARE_DB"] = DBP
os.environ["AGENT_KEY"] = "testkey123"
os.environ["SESSION_SECRET"] = "testsecret"
os.environ["MUSEFM_MODS"] = "ownerhuman"
os.environ.pop("PORT", None)
sys.path.insert(0, REPO)

import app as A  # noqa: E402
import dm as DM  # noqa: E402

db = A.db
client = A.app.test_client()
AH = {"X-Agent-Key": "testkey123"}
fails = []

def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" — " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)

def b64key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")

agent = db.register_identity("routebot", b64key())
human = db.register_identity("routehuman", b64key())
owner = db.register_identity("ownerhuman", b64key())
# Humans are distinguished from agents by password_hash
# (see _dm_peer_participant); give the humans passwords.
from werkzeug.security import generate_password_hash  # noqa: E402
db.set_identity_password(human["fm_id"], generate_password_hash("pw"))
db.set_identity_password(owner["fm_id"], generate_password_hash("pw"))

def login_as(ident):
    with client.session_transaction() as s:
        s["fm_id"] = ident["fm_id"]
        s["csrf_token"] = "tok123"

def wpost(path, body):
    return client.post(path, data=json.dumps(body),
                       content_type="application/json")

def apost(path, body):
    h = dict(AH)
    return client.post(path, data=json.dumps(body),
                       content_type="application/json", headers=h)

tkey = DM.thread_key(DM.participant_key("agent", agent["fm_id"]),
                     DM.participant_key("human", human["fm_id"]))

# 1. human -> agent web send creates a bell notification for the agent
login_as(human)
r = wpost("/api/dm/web/send", {"thread_key": tkey, "csrf_token": "tok123",
                               "body": "Hello bot, proper test message here"})
d = r.get_json()
check("human web send 200", r.status_code == 200 and d["ok"], r.status_code)
check("agent bell notification created",
      db.unread_count(agent["fm_id"]) == 1, db.unread_count(agent["fm_id"]))
row = db._one("SELECT type, text FROM notifications WHERE fm_id=? AND read=0",
              (agent["fm_id"],))
check("notification is dm type with preview",
      row and row["type"] == "dm" and "routehuman" in row["text"], row)

# 2. agent reply notifies the human (existing thread, so allowed)
r = apost("/api/dm/send", {"handle": "routebot", "to": "routehuman",
                           "body": "Hello human, replying properly"})
check("agent reply 200", r.status_code == 200, r.status_code)
check("human bell lights up", db.unread_count(human["fm_id"]) == 1,
      db.unread_count(human["fm_id"]))

# 3. human opens the thread -> bell clears
r = client.get("/api/dm/web/thread", query_string={"thread_key": tkey})
check("thread open 200", r.status_code == 200, r.status_code)
check("bell clears on thread open", db.unread_count(human["fm_id"]) == 0,
      db.unread_count(human["fm_id"]))

# 4. owner topbar badge: unseen before look, zero after
with A.app.test_request_context("/"):
    sess = {"fm_id": owner["fm_id"], "handle": "ownerhuman"}
    before = A._dm_human_unread_total(sess)
check("owner badge counts thread activity", before == 2, before)
login_as(owner)
r = client.get("/api/dm/web/thread", query_string={"thread_key": tkey})
check("owner thread open 200", r.status_code == 200, r.status_code)
with A.app.test_request_context("/"):
    sess = {"fm_id": owner["fm_id"], "handle": "ownerhuman"}
    after = A._dm_human_unread_total(sess)
check("owner badge clears after review", after == 0, after)

# 5. new reply relights the owner badge (same-second safe via id logic)
r = apost("/api/dm/send", {"handle": "routebot", "to": "routehuman",
                           "body": "one more message right away"})
check("second agent reply 200", r.status_code == 200, r.status_code)
with A.app.test_request_context("/"):
    sess = {"fm_id": owner["fm_id"], "handle": "ownerhuman"}
    relit = A._dm_human_unread_total(sess)
check("owner badge relights on new message", relit == 1, relit)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
