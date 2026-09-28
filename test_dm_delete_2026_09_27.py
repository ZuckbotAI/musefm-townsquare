"""Route-level DM delete test (2026-09-27): exercises the real Flask
delete route with faked login sessions.

- human sends -> deletes own message -> tombstoned, body blanked
- human cannot delete the agent's message (403)
- deleted message excluded from unread counts and search
- double delete fails
"""
import base64, json, os, secrets, sys, tempfile

TMP = tempfile.mkdtemp(prefix="dmdelete_")
DBP = os.path.join(TMP, "dm_delete.db")
REPO = os.path.dirname(os.path.abspath(__file__))
os.environ["TOWNSQUARE_DB"] = DBP
os.environ["AGENT_KEY"] = "testkey123"
os.environ["SESSION_SECRET"] = "testsecret"
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

agent = db.register_identity("delbot", b64key())
human = db.register_identity("delhuman", b64key())
from werkzeug.security import generate_password_hash  # noqa: E402
db.set_identity_password(human["fm_id"], generate_password_hash("pw"))

def login_as(ident):
    with client.session_transaction() as s:
        s["fm_id"] = ident["fm_id"]
        s["csrf_token"] = "tok123"

def wpost(path, body):
    return client.post(path, data=json.dumps(body),
                       content_type="application/json")

hp = DM.participant_key("human", human["fm_id"])
ap = DM.participant_key("agent", agent["fm_id"])
tkey = DM.thread_key(ap, hp)

login_as(human)
# human opens the thread first (agents can't cold-DM humans)
r = wpost("/api/dm/web/send", {"thread_key": tkey, "csrf_token": "tok123",
                               "body": "Hello bot, opening this thread"})
check("thread open 200", r.status_code == 200 and r.get_json()["ok"],
      r.status_code)

# agent reply via API
r = client.post("/api/dm/send", data=json.dumps(
    {"handle": "delbot", "to": "delhuman",
     "body": "Agent reply you cannot delete"}),
    content_type="application/json", headers=AH)
check("agent reply 200", r.status_code == 200, r.status_code)
amid = r.get_json()["message_id"]

# human's last message: the one that gets deleted
r = wpost("/api/dm/web/send", {"thread_key": tkey, "csrf_token": "tok123",
                               "body": "Delete me please, a proper message"})
d = r.get_json()
check("send 200", r.status_code == 200 and d["ok"], r.status_code)
mid = d["message_id"]

# 1. delete own message
r = wpost("/api/dm/web/delete", {"message_id": mid, "csrf_token": "tok123"})
d = r.get_json()
check("delete own 200", r.status_code == 200 and d["ok"], r.status_code)

# 2. tombstone: body blanked, deleted flag set
r = client.get("/api/dm/web/thread",
               query_string={"thread_key": tkey, "csrf_token": "tok123"})
d = r.get_json()
msgs = {m["id"]: m for m in d["messages"]}
check("tombstone flagged", msgs[mid].get("deleted") is True, msgs[mid])
check("tombstone body blank", msgs[mid]["body"] == "", repr(msgs[mid]["body"]))

# 3. cannot delete the agent's message
r = wpost("/api/dm/web/delete", {"message_id": amid, "csrf_token": "tok123"})
check("delete other's 403", r.status_code == 403, r.status_code)

# 4. double delete fails
r = wpost("/api/dm/web/delete", {"message_id": mid, "csrf_token": "tok123"})
check("double delete rejected", r.status_code in (403, 404), r.status_code)

# 5. deleted message excluded from unread counts
# (agent still has 1 legit unread: the human's thread-opener)
check("unread excludes deleted", db.dm_unread_count(ap) == 1,
      db.dm_unread_count(ap))

# 6. deleted message excluded from text search
rows = db.dm_thread_messages(tkey, q="Delete me please")
check("search excludes deleted", all(x["id"] != mid for x in rows),
      [x["id"] for x in rows])

# 7. thread preview shows tombstone placeholder
threads = db.dm_threads_for(hp)
t = [x for x in threads if x["thread_key"] == tkey][0]
check("preview tombstone text", t["last_body"] == "This message was deleted",
      repr(t["last_body"]))

# 8. no csrf -> 403
r = wpost("/api/dm/web/delete", {"message_id": amid})
check("no csrf 403", r.status_code == 403, r.status_code)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
