"""Tester-loop 2026-09-21 (06:35 run): six new P2s from human + adversarial QA,
all code-confirmed. Tests only — proves the bugs; no source changes.

1. No Cache-Control on API responses (e.g. /api/ping) — dynamic JSON could be
   cached by intermediaries; expect `Cache-Control: no-store` on /api/*.
2. GET /api/agents/<unknown>/activity -> 200 echoing the raw id, while
   /api/identity/<unknown>, /api/pond/<unknown>, /api/rewards/<unknown>
   all 404. Expect 404.
3. Trailing slash /api/forum/react/ -> 404 but double slash /api//forum/react
   -> 405. Expect the two to agree.
4. Display-name charset rejects apostrophes: "Bob O'Brien" -> 400. Expect
   real human names to be accepted (design P2, not a vuln).
5. Missing `timestamp` in a signed body reports "bad timestamp" (identity.py
   maps None -> int() TypeError -> "bad timestamp"). Expect "missing timestamp".
6. Duplicate JSON keys silently last-win (std json behavior; noting only).

Run: python3 test_testerloop_p2_2026_09_21_b.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app as appmod
from identity import signed_body

TEST_DB = "/tmp/test-townsquare-p2-2026-09-21-b.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def main():
    client = setup()

    # --- P2 1: Cache-Control on API responses
    r = client.get("/api/ping", environ_overrides={"REMOTE_ADDR": "10.211.0.1"})
    cc = r.headers.get("Cache-Control", "")
    check("api responses carry Cache-Control: no-store",
          "no-store" in cc, f"/api/ping Cache-Control={cc!r}")

    # --- P2 2: unknown identity activity -> 200 instead of 404
    r = client.get("/api/agents/fm_no_such_muse_xyz/activity",
                   environ_overrides={"REMOTE_ADDR": "10.211.0.2"})
    check("unknown identity activity -> 404 (not 200)",
          r.status_code == 404,
          f"status={r.status_code} body={r.get_data(as_text=True)[:80]}")

    # --- P2 3: trailing-slash vs double-slash disagree
    r1 = client.get("/api/forum/react/",
                    environ_overrides={"REMOTE_ADDR": "10.211.0.3"})
    r2 = client.get("/api//forum/react",
                    environ_overrides={"REMOTE_ADDR": "10.211.0.4"})
    check("trailing-slash and double-slash paths agree",
          r1.status_code == r2.status_code,
          f"/api/forum/react/ -> {r1.status_code}, /api//forum/react -> {r2.status_code}")

    # --- P2 4: display names reject apostrophes
    try:
        appmod.db.set_identity_display_name  # exists?
        # register via API first
        priv = Ed25519PrivateKey.generate()
        pub = b64u(priv.public_key().public_bytes_raw())
        r = client.post("/api/identity/register",
                        json={"handle": "p2apostrophe", "public_key": pub},
                        environ_overrides={"REMOTE_ADDR": "10.211.0.5"})
        fm_id = r.get_json()["fm_id"]
        appmod.db.set_identity_display_name(fm_id, "Bob O'Brien")
        check("display name accepts apostrophes", True)
    except ValueError as e:
        check("display name accepts apostrophes", False, str(e))

    # --- P2 5: missing timestamp says "bad timestamp" instead of "missing timestamp"
    priv2 = Ed25519PrivateKey.generate()
    pub2 = b64u(priv2.public_key().public_bytes_raw())
    r = client.post("/api/identity/register",
                    json={"handle": "p2notimestamp", "public_key": pub2},
                    environ_overrides={"REMOTE_ADDR": "10.211.0.6"})
    fm_id = r.get_json()["fm_id"]
    priv_b64 = b64u(priv2.private_bytes_raw())
    body = signed_body(priv_b64, "trustline_status", fm_id)
    body.pop("timestamp", None)  # signature now covers a timestamp the query lacks;
    # the timestamp check fires before signature verification -> "bad timestamp"
    # (GET route: signed fields ride the query string)
    r = client.get("/api/trustline/status", query_string=body,
                   environ_overrides={"REMOTE_ADDR": "10.211.0.7"})
    err = (r.get_json() or {}).get("error", "")
    check("missing timestamp reports 'missing timestamp'",
          "missing timestamp" in err, f"error={err!r}")

    # --- P2 6: duplicate JSON keys last-win (noting only)
    r = client.post("/api/identity/register",
                    data='{"handle": "firsthandle", "handle": "seconddup", "public_key": "%s"}' % pub,
                    content_type="application/json",
                    environ_overrides={"REMOTE_ADDR": "10.211.0.8"})
    j = r.get_json() or {}
    check("duplicate JSON keys do not silently last-win",
          j.get("handle") != "seconddup",
          f"registered handle={j.get('handle')!r}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)


if __name__ == "__main__":
    main()
