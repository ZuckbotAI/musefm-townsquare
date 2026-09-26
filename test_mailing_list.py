#!/usr/bin/env python3
"""Tests for the email alerts mailing list (db.py + auth_email.py +
mailing.py + app.py routes).

Covers:
  1. GET /newsletter renders the signup form.
  2. POST /newsletter validation: garbage -> 400, no row created.
  3. POST /newsletter happy path: row created as pending_optin (double
     opt-in); the confirmation email is attempted via the SMTP helper.
  4. Double opt-in confirm: GET /newsletter/confirm?token= flips
     pending_optin -> subscribed; tampered token -> 400.
  5. POST /newsletter for an already-subscribed address -> "already on
     the list", no duplicate, no new confirm email.
  6. Unsubscribe: signed per-email token -> unsubscribed; bad token -> 400.
  7. import_account_emails: account emails land auto-subscribed; input
     deduped case-insensitively; blanks/invalid skipped; an address that
     already unsubscribed is NEVER resubscribed.
  8. send_alert: only subscribed addresses receive; every email carries a
     one-click unsubscribe link.
  9. Admin route POST /api/admin/mailing/send: 401 without the agent key,
     200 with it; only subscribed addresses get the alert.

Run: .venv/bin/python test_mailing_list.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["AGENT_KEY"] = "testkey123"
os.environ["SESSION_SECRET"] = "testsecret-mailing"
os.environ.pop("SMTP_HOST", None)  # hermetic: no real email ever sends

import app as appmod
import auth_email
import mailing
from db import ensure_mailing_list_schema, import_account_emails

TEST_DB = "/tmp/test-townsquare-mailing.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.204.0.%d" % _ip[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


AH = {"X-Agent-Key": "testkey123"}


class SendCapture:
    """Stand-in for auth_email.send_simple_email: records calls, sends nothing."""

    def __init__(self):
        self.calls = []
        self._orig = auth_email.send_simple_email

    def __enter__(self):
        def fake(to_email, subject, text, html=None):
            self.calls.append({"to": to_email, "subject": subject,
                               "text": text, "html": html})
            return True, None
        auth_email.send_simple_email = fake
        return self

    def __exit__(self, *a):
        auth_email.send_simple_email = self._orig
        return False


def test_form_page(client):
    r = client.get("/newsletter")
    check("GET /newsletter -> 200 with form",
          r.status_code == 200 and b'name="email"' in r.data,
          r.status_code)


def test_validation(client):
    for bad in ["", "not-an-email", "a" * 250 + "@x.co", "no-at-sign-here"]:
        r = client.post("/newsletter", data={"email": bad},
                        environ_base=fresh_ip())
        if r.status_code != 400:
            check("POST /newsletter rejects %r" % bad[:20], False,
                  r.status_code)
            return
    check("POST /newsletter rejects bad emails with 400", True)
    check("no mailing row created for bad email",
          appmod.db.mailing_get("not-an-email") is None)


def test_double_optin(client):
    email = "optin@example.test"
    with SendCapture() as cap:
        r = client.post("/newsletter", data={"email": email},
                        environ_base=fresh_ip())
    check("POST /newsletter -> 200", r.status_code == 200, r.status_code)
    row = appmod.db.mailing_get(email)
    check("row created as pending_optin",
          row is not None and row["status"] == "pending_optin" and
          row["source"] == "newsletter_form" and row["confirmed_at"] is None,
          row)
    check("confirmation email attempted", len(cap.calls) == 1 and
          cap.calls[0]["to"] == email, len(cap.calls))
    check("confirm email has a link",
          "/newsletter/confirm?token=" in (cap.calls[0]["text"] if cap.calls
                                           else ""))

    token = auth_email.make_newsletter_token(appmod.app.secret_key, email)
    r = client.get("/newsletter/confirm?token=" + token)
    check("GET /newsletter/confirm -> 200", r.status_code == 200,
          r.status_code)
    row = appmod.db.mailing_get(email)
    check("confirm flips to subscribed with confirmed_at",
          row["status"] == "subscribed" and row["confirmed_at"] is not None,
          row)

    r = client.get("/newsletter/confirm?token=" + token + "tampered")
    check("tampered confirm token -> 400", r.status_code == 400,
          r.status_code)


def test_already_subscribed(client):
    email = "optin@example.test"  # subscribed by the previous test
    with SendCapture() as cap:
        r = client.post("/newsletter", data={"email": email},
                        environ_base=fresh_ip())
    check("re-POST subscribed -> 200 already-on-list page",
          r.status_code == 200 and b"Already on the list" in r.data,
          r.status_code)
    check("no second confirm email sent", len(cap.calls) == 0, len(cap.calls))
    rows = appmod.db._q("SELECT COUNT(*) c FROM mailing_list WHERE email=?",
                        (email,))
    check("no duplicate row", rows[0]["c"] == 1, rows[0]["c"])


def test_unsubscribe(client):
    email = "optin@example.test"
    token = auth_email.make_unsubscribe_token(appmod.app.secret_key, email)
    r = client.get("/newsletter/unsubscribe?token=" + token)
    check("GET /newsletter/unsubscribe -> 200", r.status_code == 200,
          r.status_code)
    row = appmod.db.mailing_get(email)
    check("row flipped to unsubscribed with timestamp",
          row["status"] == "unsubscribed" and
          row["unsubscribed_at"] is not None, row)

    r = client.get("/newsletter/unsubscribe?token=bogus")
    check("bad unsubscribe token -> 400", r.status_code == 400,
          r.status_code)


def test_import_idempotent(client):
    db = appmod.db
    # seed: one subscribed, one unsubscribed, one pending
    db.mailing_request_subscribe("keep@example.test")
    db.mailing_confirm("keep@example.test")
    db.mailing_request_subscribe("gone@example.test")
    db.mailing_unsubscribe("gone@example.test")
    db.mailing_request_subscribe("waiting@example.test")

    res = import_account_emails(db, [
        "new1@example.test", "NEW1@example.test",  # dup pair, case differs
        "  new2@example.test  ",                   # whitespace trimmed
        "", "   ", "bad-email",                    # invalid
        "keep@example.test",                       # already subscribed
        "gone@example.test",                       # UNSUBSCRIBED: must stay out
        "waiting@example.test",                    # pending: keep its own flow
    ])
    check("import counts",
          res == {"added": 2, "skipped_existing": 3, "skipped_invalid": 3},
          res)
    check("imported addresses auto-subscribed",
          db.mailing_get("new1@example.test")["status"] == "subscribed" and
          db.mailing_get("new2@example.test")["status"] == "subscribed" and
          db.mailing_get("new1@example.test")["source"] == "account_import" and
          db.mailing_get("new1@example.test")["confirmed_at"] is not None)
    check("unsubscribed address NEVER resubscribed",
          db.mailing_get("gone@example.test")["status"] == "unsubscribed")
    check("pending address keeps its own flow",
          db.mailing_get("waiting@example.test")["status"] == "pending_optin")


def test_send_alert_only_subscribed():
    db = appmod.db
    with SendCapture() as cap:
        res = mailing.send_alert(db, "Test alert", "Hello town.",
                                 "http://localhost", appmod.app.secret_key)
    got = sorted(c["to"] for c in cap.calls)
    # subscribed: keep@, new1@, new2@ (optin@ unsubscribed, waiting@ pending)
    check("send_alert hits only subscribed",
          got == ["keep@example.test", "new1@example.test",
                  "new2@example.test"], got)
    check("send_alert result counts",
          res["sent"] == 3 and res["failed"] == [], res)
    ok_links = all("/newsletter/unsubscribe?token=" in c["text"] and
                   "/newsletter/unsubscribe?token=" in (c["html"] or "")
                   for c in cap.calls)
    check("every alert carries one-click unsubscribe link", ok_links)
    # the unsubscribe token in the footer must actually work
    first_html = cap.calls[0]["html"]
    token = first_html.split("/newsletter/unsubscribe?token=")[1].split('"')[0]
    check("footer token verifies to the recipient",
          auth_email.read_unsubscribe_token(appmod.app.secret_key, token) ==
          cap.calls[0]["to"])


def test_admin_send_route(client):
    payload = {"subject": "Hi", "body_text": "Town news."}
    r = client.post("/api/admin/mailing/send", json=payload,
                    environ_base=fresh_ip())
    check("admin send without agent key -> 401", r.status_code == 401,
          r.status_code)

    r = client.post("/api/admin/mailing/send", json={}, headers=AH,
                    environ_base=fresh_ip())
    check("admin send missing fields -> 400", r.status_code == 400,
          r.status_code)

    with SendCapture() as cap:
        r = client.post("/api/admin/mailing/send", json=payload, headers=AH,
                        environ_base=fresh_ip())
    body = r.get_json() or {}
    check("admin send with key -> 200", r.status_code == 200, r.status_code)
    check("admin send reports sent count",
          body.get("ok") is True and body.get("sent") == 3, body)
    check("admin send delivered only to subscribed",
          sorted(c["to"] for c in cap.calls) ==
          ["keep@example.test", "new1@example.test", "new2@example.test"],
          [c["to"] for c in cap.calls])


def test_backfill_on_schema_init():
    # ensure_mailing_list_schema auto-subscribes verified account emails
    # (deploy-time import of the missed ones). Idempotent, and never
    # resubscribes an unsubscribed address.
    db = appmod.db
    now = int(time.time())
    db._exec("INSERT INTO identities"
             " (fm_id, handle, public_key, created_at, email, email_verified)"
             " VALUES ('fm_backfill1', 'backfill_one', 'k', ?,"
             " 'Backfill@Example.Test', 1)", (now,))
    db._exec("INSERT INTO identities"
             " (fm_id, handle, public_key, created_at, email, email_verified)"
             " VALUES ('fm_backfill2', 'backfill_two', 'k', ?,"
             " 'unverified@example.test', 0)", (now,))
    ensure_mailing_list_schema(db)
    row = db.mailing_get("backfill@example.test")
    check("backfill subscribes verified account email",
          row is not None and row["status"] == "subscribed"
          and row["source"] == "account_import", row)
    check("backfill skips unverified account email",
          db.mailing_get("unverified@example.test") is None)
    ensure_mailing_list_schema(db)
    n = db.db.execute("SELECT COUNT(*) FROM mailing_list"
                      " WHERE email='backfill@example.test'").fetchone()[0]
    check("backfill is idempotent (no duplicate row)", n == 1, n)
    db._exec("UPDATE mailing_list SET status='unsubscribed',"
             " unsubscribed_at=? WHERE email='backfill@example.test'",
             (now,))
    ensure_mailing_list_schema(db)
    check("backfill never resubscribes an unsubscribed address",
          db.mailing_get("backfill@example.test")["status"] == "unsubscribed")


def test_verify_hook_autosubscribes():
    # New accounts: verifying an email auto-subscribes it to alerts, so
    # capture keeps working for promos going forward.
    db = appmod.db
    now = int(time.time())
    db._exec("INSERT INTO identities"
             " (fm_id, handle, public_key, created_at, email, email_verified)"
             " VALUES ('fm_hook1', 'hook_one', 'k', ?,"
             " 'hook@example.test', 0)", (now,))
    check("unverified email not in list yet",
          db.mailing_get("hook@example.test") is None)
    db.mark_email_verified("fm_hook1")
    row = db.mailing_get("hook@example.test")
    check("mark_email_verified auto-subscribes the email",
          row is not None and row["status"] == "subscribed", row)


def main():
    client = setup()
    test_form_page(client)
    test_validation(client)
    test_double_optin(client)
    test_already_subscribed(client)
    test_unsubscribe(client)
    test_import_idempotent(client)
    test_send_alert_only_subscribed()
    test_admin_send_route(client)
    test_backfill_on_schema_init()
    test_verify_hook_autosubscribes()
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
