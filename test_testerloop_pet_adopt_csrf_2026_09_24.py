#!/usr/bin/env python3
"""
Proving test for the NEW P1 found in the 2026-09-24 21:35 tester loop
(human tester, coordinator re-verified live + in code):

POST /pet/adopt accepts the web form WITHOUT a csrf_token (302 -> pet
adopted, DB-proven). Every sibling pet endpoint enforces CSRF:
pet_web_rename calls _check_csrf() directly; hatch/reroll/cure go through
_pet_web_simple(), which checks. pet_web_adopt() (app.py:4054) never calls
it. Same hole class on the mod POST surface (code-verified, not
live-tested): /mod/flags/<id>/resolve, /mod/flags/bulk-resolve,
/mod/uploads/<kind>/<uid>/<action>, /mod/uploads/bulk all call
_require_mod() but never _check_csrf().

Repro (coordinator, live on the 21:35 scratch instance):
  signup -> POST /pet/adopt {species: bloop, name: csrfvictim} (no token)
  -> 302; tidepals table contains the adopted pet.

Tests only — no app source changes. The first check is expected to FAIL
on the current checkout; the control must PASS both before and after the fix.

Run:  python3 test_testerloop_pet_adopt_csrf_2026_09_24.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
from db import Database
import pets as petsmod

TEST_DB = "/tmp/test-townsquare-tl242135-petadopt-csrf.db"
TEST_DATA = "/tmp/test-townsquare-tl242135-petadopt-csrf-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def fresh_client():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)  # full schema + seeds, mirrors app startup
    petsmod.ensure_pet_schema(appmod.db)  # pet tables aren't in init_db
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()
    return c


def main():
    c = fresh_client()
    r = c.post("/signup", data={
        "handle": "adoptcsrf", "email": "adoptcsrf@example.test",
        "password": "Testpw12345!", "password_confirm": "Testpw12345!"})
    check("signup works", r.status_code in (200, 302), r.status_code)
    # signup does NOT log in (signup_success.html); log in explicitly
    r = c.post("/login", data={"handle": "adoptcsrf",
                               "password": "Testpw12345!"})
    check("login works", r.status_code in (200, 302), r.status_code)
    html = c.get("/").get_data(as_text=True)
    check("session is logged in", "adoptcsrf" in html)

    # --- the bug: adopt WITHOUT csrf_token ---
    r = c.post("/pet/adopt", data={"species": "bloop", "name": "csrfvictim"})
    check("adopt without csrf_token is REJECTED (403)",
          r.status_code == 403, r.status_code)
    n = appmod.db._one(
        "SELECT COUNT(*) n FROM tidepals WHERE name='csrfvictim'")["n"]
    check("rejected adopt stores no pet", n == 0, f"count={n}")

    # --- control: adopt WITH a valid token still works (302) ---
    c2 = fresh_client()
    c2.post("/signup", data={
        "handle": "adoptcsrf2", "email": "adoptcsrf2@example.test",
        "password": "Testpw12345!", "password_confirm": "Testpw12345!"})
    c2.post("/login", data={"handle": "adoptcsrf2",
                            "password": "Testpw12345!"})
    with c2.session_transaction() as sess:
        pass  # session may not have a token yet; grab it from a page meta tag
    import re as _re
    html = c2.get("/pet").get_data(as_text=True)
    m = _re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    tok = m.group(1) if m else ""
    check("page carries a csrf token for logged-in user", bool(tok))
    r = c2.post("/pet/adopt", data={"species": "bloop", "name": "legitpet",
                                    "csrf_token": tok})
    check("adopt with valid token succeeds (302)", r.status_code == 302,
          r.status_code)
    n = appmod.db._one(
        "SELECT COUNT(*) n FROM tidepals WHERE name='legitpet'")["n"]
    check("valid adopt stores the pet", n == 1, f"count={n}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
