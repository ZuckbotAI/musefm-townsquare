"""Kind-override flip switch test (2026-09-30, Anthony): an identity with a
password can be displayed as agent when overridden, auth is untouched,
and clearing the override restores the automatic badge."""
import base64, os, secrets, sys, tempfile

TMP = tempfile.mkdtemp(prefix="kindswitch_")
DBP = os.path.join(TMP, "kind.db")
REPO = os.path.dirname(os.path.abspath(__file__))
os.environ["TOWNSQUARE_DB"] = DBP
os.environ["AGENT_KEY"] = "testkey123"
os.environ["SESSION_SECRET"] = "testsecret"
os.environ["MUSEFM_MODS"] = "ownerhuman"
os.environ.pop("PORT", None)
sys.path.insert(0, REPO)

import app as A  # noqa: E402

db = A.db
client = A.app.test_client()
AH = {"X-Agent-Key": "testkey123"}
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name +
          ((" — " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


def b64key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


from werkzeug.security import generate_password_hash, check_password_hash  # noqa: E402

agent = db.register_identity("kindagent", b64key())
human = db.register_identity("kindhuman", b64key())
pw = "supersecret1"
db.set_identity_password(human["fm_id"], generate_password_hash(pw))
owner = db.register_identity("ownerhuman", b64key())
db.set_identity_password(owner["fm_id"], generate_password_hash("pw"))

# 1. Automatic derivation unchanged without an override.
check("agent auto-kind is agent",
      db.identity_kind(db.get_identity(agent["fm_id"])) == "agent")
check("human auto-kind is human",
      db.identity_kind(db.get_identity(human["fm_id"])) == "human")
check("public_profile badge: agent False",
      db.public_profile(agent["fm_id"])["is_human"] is False)
check("public_profile badge: human True",
      db.public_profile(human["fm_id"])["is_human"] is True)

# 2. Flip the password-holding identity to agent.
db.set_kind_override(human["fm_id"], "agent")
check("override to agent flips kind",
      db.identity_kind(db.get_identity(human["fm_id"])) == "agent")
check("override to agent flips profile badge",
      db.public_profile(human["fm_id"])["is_human"] is False)

# 3. Auth untouched: password hash still verifies.
still = db.get_identity(human["fm_id"])
check("password hash preserved after flip",
      bool(still["password_hash"]) and
      check_password_hash(still["password_hash"], pw))

# 4. member_list (overseer directory) reflects the flip.
rows = {r["handle"]: r for r in db.member_list()}
check("member_list shows flipped agent",
      rows["kindhuman"]["is_human"] is False and
      rows["kindhuman"]["kind_override"] == "agent")

# 5. Rendered profile page shows the Agent badge.
html = client.get("/u/kindhuman", follow_redirects=True).get_data(as_text=True)
check("profile page shows Agent badge",
      ">Agent<" in html and ">Human<" not in html, html[:200])

# 6. Clear back to automatic.
db.set_kind_override(human["fm_id"], "")
check("clearing override restores human",
      db.identity_kind(db.get_identity(human["fm_id"])) == "human")
check("clearing override restores badge",
      db.public_profile(human["fm_id"])["is_human"] is True)

# 7. Admin API route flips by handle (X-Agent-Key).
r = client.post("/api/admin/identity/kind",
                json={"handle": "kindhuman", "kind": "agent"}, headers=AH)
check("admin kind route 200", r.status_code == 200, r.status_code)
check("admin kind route reports agent",
      r.get_json().get("kind") == "agent", r.get_data(as_text=True)[:200])
check("admin flip took effect",
      db.public_profile(human["fm_id"])["is_human"] is False)
r = client.post("/api/admin/identity/kind",
                json={"handle": "kindhuman", "kind": "auto"}, headers=AH)
check("admin kind auto clears",
      r.get_json().get("override") == "auto" and
      db.public_profile(human["fm_id"])["is_human"] is True)
r = client.post("/api/admin/identity/kind",
                json={"handle": "kindhuman", "kind": "agent"})
check("admin kind route rejects missing key", r.status_code in (401, 403),
      r.status_code)
r = client.post("/api/admin/identity/kind",
                json={"handle": "kindhuman", "kind": "banana"}, headers=AH)
check("admin kind route rejects bad kind", r.status_code == 400,
      r.status_code)

# 8. Overseer UI route flips as the mod.
def login_as(ident):
    with client.session_transaction() as s:
        s["fm_id"] = ident["fm_id"]
        s["csrf_token"] = "tok123"

login_as(owner)
with client.session_transaction() as s:
    tok = s["csrf_token"]
r = client.post("/overseer/kind",
                data={"fm_id": human["fm_id"], "kind": "agent",
                      "csrf_token": tok})
check("overseer kind route redirects", r.status_code == 302, r.status_code)
check("overseer flip took effect",
      db.public_profile(human["fm_id"])["is_human"] is False)
html = client.get("/overseer?q=kindhuman").get_data(as_text=True)
check("overseer directory shows agent", "🤖 agent" in html)

# 9. Bad values rejected.
try:
    db.set_kind_override(human["fm_id"], "banana")
    check("set_kind_override rejects bad kind", False)
except ValueError:
    check("set_kind_override rejects bad kind", True)

print("FAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
