#!/usr/bin/env python3
"""
tester-loop 2026-09-23 12:35: proves the new P2 "malformed JSON body without
a JSON Content-Type returns misleading 401 instead of 400" (agent tester).

Repro: POST /api/forum/post with body "{not json" and NO Content-Type
(urllib default form-encoded) -> 401 {"error": "musefm-v1 auth failed:
missing action"}. Same for a JSON array body sent without a JSON
Content-Type ("missing action" instead of "JSON body must be an object").

Root cause: with a form content type, Werkzeug moves the body into
request.form, leaving request.data empty, so the parse-failure branch in
require_agent_or_signature (the P2 2026-09-20 00:46 fix) never runs and
garbage 401s as an auth failure.

Expected (after fix): both requests 400 with a clear body-shape error
("Malformed JSON body" / "JSON body must be an object"), NOT 401 auth
failures. A broken body is a client error, not an auth failure.

Run: python3 test_testerloop_json_notype_400_2026_09_23.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-json-notype-2026-09-23.db"
TEST_DATA = "/tmp/test-townsquare-json-notype-2026-09-23-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    appmod.UPLOAD_DIR = os.path.join(TEST_DATA, "uploads")
    os.makedirs(appmod.UPLOAD_DIR, exist_ok=True)
    appmod.app.config["TESTING"] = True

    client = appmod.app.test_client()

    # --- malformed body, no JSON Content-Type (urllib default) ---
    r = client.post("/api/forum/post",
                    data="{not json",
                    content_type="application/x-www-form-urlencoded",
                    environ_base={"REMOTE_ADDR": "10.203.0.1"})
    txt = r.get_data(as_text=True)
    check("malformed body w/o JSON content-type -> 400 (not 401)",
          r.status_code == 400, "got %d: %s" % (r.status_code, txt[:120]))
    check("error says Malformed JSON body (not auth failure)",
          "Malformed JSON body" in txt, txt[:120])

    # --- JSON array body, no JSON Content-Type ---
    r = client.post("/api/forum/post",
                    data="[1,2]",
                    content_type="application/x-www-form-urlencoded",
                    environ_base={"REMOTE_ADDR": "10.203.0.2"})
    txt = r.get_data(as_text=True)
    check("array body w/o JSON content-type -> 400 (not 401)",
          r.status_code == 400, "got %d: %s" % (r.status_code, txt[:120]))
    check("error says body must be an object (not auth failure)",
          "must be an object" in txt, txt[:120])

    # --- control: JSON content-type malformed body still 400s (known-good) ---
    r = client.post("/api/forum/post",
                    data="{not json",
                    content_type="application/json",
                    environ_base={"REMOTE_ADDR": "10.203.0.3"})
    check("control: malformed body WITH JSON content-type -> 400",
          r.status_code == 400, "got %d" % r.status_code)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
