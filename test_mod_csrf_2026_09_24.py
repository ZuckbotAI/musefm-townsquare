"""P1 security: mod POST routes must reject missing/invalid CSRF tokens.

Covers the four mod queue POST routes fixed 2026-09-24 alongside
/pet/adopt (which has its own proving test):
  - POST /mod/flags/<id>/resolve      (single flag resolution)
  - POST /mod/flags/bulk-resolve      (bulk flag resolution)
  - POST /mod/uploads/<kind>/<uid>/<action>  (single upload approve/reject)
  - POST /mod/uploads/bulk            (bulk upload approve/reject)

For each: no token -> 403; bad token -> 403; valid token -> success and
the state change actually lands. Uses a throwaway SQLite db + Flask test
client. Nothing touches townsquare.db.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/home/hatch/workspace/pets-new-universe")
sys.path.insert(0, HERE)

import app as appmod

TEST_DB = "/tmp/test-townsquare-mod-csrf-%d.db" % os.getpid()

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          ((" — " + str(extra)) if extra and not cond else ""))


def fresh_client():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def login_mod(client):
    r = client.post("/signup", data={"handle": "ModHuman",
                                     "email": "modhuman@example.com",
                                     "password": "pw123456",
                                     "password_confirm": "pw123456"})
    assert r.status_code in (200, 302), r.status_code
    r = client.post("/login", data={"handle": "ModHuman",
                                    "password": "pw123456"})
    assert r.status_code == 302, r.status_code
    return client


def csrf_token_for(client):
    r = client.get("/mod/flags")
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf meta token rendered on /mod/flags"
    return m.group(1)


def seed_flag(db, target_id="p1"):
    cur = db._exec(
        "INSERT INTO post_flags (target_type, target_id, flagger_fm_id,"
        " flagger_handle, reason, created_at, status)"
        " VALUES (?,?,?,?,?,?, 'open')",
        ("post", target_id, "fm_reporter", "reporter", "spam", 1))
    return cur.lastrowid


def seed_photo(db):
    cur = db._exec(
        "INSERT INTO photos (title, img_path, created_at, status)"
        " VALUES (?,?,?, 'pending')",
        ("t", "img/x.png", 1))
    return cur.lastrowid


def main():
    c = login_mod(fresh_client())
    tok = csrf_token_for(c)

    # --- 1. single flag resolve --------------------------------------
    fid = seed_flag(appmod.db)
    r = c.post(f"/mod/flags/{fid}/resolve", data={"action": "dismissed"})
    check("flag resolve without token -> 403", r.status_code == 403,
          r.status_code)
    r = c.post(f"/mod/flags/{fid}/resolve",
               data={"action": "dismissed", "csrf_token": "bad-token"})
    check("flag resolve with bad token -> 403", r.status_code == 403,
          r.status_code)
    r = c.post(f"/mod/flags/{fid}/resolve",
               data={"action": "dismissed", "csrf_token": tok})
    check("flag resolve with valid token succeeds",
          r.status_code in (200, 302), r.status_code)
    st = appmod.db._one("SELECT status FROM post_flags WHERE id = ?",
                        (fid,))["status"]
    check("flag status changed to dismissed", st == "dismissed", st)

    # --- 2. bulk flag resolve -----------------------------------------
    fid2 = seed_flag(appmod.db, "p2")
    r = c.post("/mod/flags/bulk-resolve",
               data={"ids": str(fid2), "action": "actioned"})
    check("bulk flag resolve without token -> 403", r.status_code == 403,
          r.status_code)
    r = c.post("/mod/flags/bulk-resolve",
               data={"ids": str(fid2), "action": "actioned",
                     "csrf_token": "bad-token"})
    check("bulk flag resolve with bad token -> 403", r.status_code == 403,
          r.status_code)
    r = c.post("/mod/flags/bulk-resolve",
               data={"ids": str(fid2), "action": "actioned",
                     "csrf_token": tok})
    j = r.get_json() or {}
    check("bulk flag resolve with valid token ok",
          r.status_code == 200 and j.get("ok") is True,
          (r.status_code, j))
    st = appmod.db._one("SELECT status FROM post_flags WHERE id = ?",
                        (fid2,))["status"]
    check("bulk flag status changed to actioned", st == "actioned", st)

    # --- 3. single upload approve --------------------------------------
    pid = seed_photo(appmod.db)
    r = c.post(f"/mod/uploads/photo/{pid}/approve")
    check("upload approve without token -> 403", r.status_code == 403,
          r.status_code)
    r = c.post(f"/mod/uploads/photo/{pid}/approve",
               data={"csrf_token": "bad-token"})
    check("upload approve with bad token -> 403", r.status_code == 403,
          r.status_code)
    r = c.post(f"/mod/uploads/photo/{pid}/approve",
               data={"csrf_token": tok})
    check("upload approve with valid token succeeds",
          r.status_code in (200, 302), r.status_code)
    st = appmod.db._one("SELECT status FROM photos WHERE id = ?",
                        (pid,))["status"]
    check("upload status changed to approved", st == "approved", st)

    # --- 4. bulk upload reject -----------------------------------------
    pid2 = seed_photo(appmod.db)
    r = c.post("/mod/uploads/bulk",
               data={"kind": "photo", "ids": str(pid2),
                     "action": "reject"})
    check("bulk upload reject without token -> 403", r.status_code == 403,
          r.status_code)
    r = c.post("/mod/uploads/bulk",
               data={"kind": "photo", "ids": str(pid2), "action": "reject",
                     "csrf_token": "bad-token"})
    check("bulk upload reject with bad token -> 403", r.status_code == 403,
          r.status_code)
    r = c.post("/mod/uploads/bulk",
               data={"kind": "photo", "ids": str(pid2), "action": "reject",
                     "csrf_token": tok})
    j = r.get_json() or {}
    check("bulk upload reject with valid token ok",
          r.status_code == 200 and j.get("ok") is True,
          (r.status_code, j))
    st = appmod.db._one("SELECT status FROM photos WHERE id = ?",
                        (pid2,))["status"]
    check("bulk upload status changed to rejected", st == "rejected", st)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    # mod access is a handle allowlist; make the test human a mod.
    os.environ["MUSEFM_MODS"] = "ModHuman"
    sys.exit(main())
