#!/usr/bin/env python3
"""End-to-end test for the Workroom pilot API (test scaffolding).

Covers: bearer-key issuance + auth, task create/list/claim/update/abandon,
lease auto-expiry, session-auth path, validation errors, and a regression
check that existing routes (/api/agents) still work.
Throwaway DB; nothing touches the real townsquare.db.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import workroom
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-workroom-pilot.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.203.0.%d" % _ip[0]}


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    workroom.ensure_workroom_schema(appmod.db)
    workroom.ensure_pilot_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    print("== schema ==")
    tables = {r[0] for r in
              appmod.db.db.execute(
                  "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("pilot_tasks", "pilot_task_history", "pilot_agent_keys"):
        check(f"table {t} exists", t in tables)

    print("== key issuance (runtime, never hardcoded) ==")
    keyA = workroom.issue_pilot_key(appmod.db, "harbor-scout-1", "fm_p1")
    keyB = workroom.issue_pilot_key(appmod.db, "harbor-scout-2", "fm_p2")
    keyC = workroom.issue_pilot_key(appmod.db, "harbor-scout-3", "fm_p3")
    check("three distinct keys minted",
          len({keyA, keyB, keyC}) == 3 and all(k.startswith("wrp_") for k in (keyA, keyB, keyC)))
    stored = [r[0] for r in appmod.db.db.execute(
        "SELECT key_hash FROM pilot_agent_keys").fetchall()]
    check("raw keys never stored in DB",
          all(k not in stored for k in (keyA, keyB, keyC)) and len(stored) == 3)

    print("== auth gating ==")
    r = c.post("/api/workroom/tasks", json={"title": "x", "difficulty": 1},
               environ_base=fresh_ip())
    check("no auth -> 401", r.status_code == 401, r.status_code)
    r = c.post("/api/workroom/tasks", json={"title": "x", "difficulty": 1},
               headers=bearer("wrp_bogus"), environ_base=fresh_ip())
    check("bad key -> 401", r.status_code == 401, r.status_code)
    r = c.post("/api/workroom/tasks/claim", json={"task_id": 1},
               headers=bearer("wrp_bogus"), environ_base=fresh_ip())
    check("bad key on claim -> 401", r.status_code == 401, r.status_code)

    print("== create task ==")
    r = c.post("/api/workroom/tasks",
               json={"title": "Fix upload mime check",
                     "description": "reject non-audio bytes labeled audio/mpeg",
                     "difficulty": 4},
               headers=bearer(keyA), environ_base=fresh_ip())
    d = r.get_json()
    check("create 200 + task_id", r.status_code == 200 and d.get("task_id") == 1,
          r.get_data(as_text=True)[:120])
    r = c.post("/api/workroom/tasks",
               json={"title": "bad diff", "difficulty": 9},
               headers=bearer(keyA), environ_base=fresh_ip())
    check("difficulty 9 -> 400", r.status_code == 400, r.status_code)
    r = c.post("/api/workroom/tasks",
               json={"title": "", "difficulty": 2},
               headers=bearer(keyA), environ_base=fresh_ip())
    check("empty title -> 400", r.status_code == 400, r.status_code)

    print("== list tasks (public read) ==")
    r = c.get("/api/workroom/tasks")
    d = r.get_json()
    t = d["tasks"][0] if d.get("tasks") else {}
    check("public list shows task open",
          r.status_code == 200 and t.get("status") == "open"
          and t.get("difficulty") == 4 and t.get("title") == "Fix upload mime check",
          r.status_code)
    check("list embeds history with created entry",
          any(h["action"] == "created" for h in t.get("history", [])))

    print("== claim ==")
    r = c.post("/api/workroom/tasks/claim", json={"task_id": 1},
               headers=bearer(keyB), environ_base=fresh_ip())
    d = r.get_json()
    check("claim 200 + lease expiry",
          r.status_code == 200 and d.get("lease_expires_at", 0) > time.time(),
          r.get_data(as_text=True)[:120])
    r = c.post("/api/workroom/tasks/claim", json={"task_id": 1},
               headers=bearer(keyC), environ_base=fresh_ip())
    check("double claim -> 400", r.status_code == 400, r.status_code)
    r = c.post("/api/workroom/tasks/claim", json={"task_id": 999},
               headers=bearer(keyC), environ_base=fresh_ip())
    check("claim missing task -> 400", r.status_code == 400, r.status_code)
    r = c.get("/api/workroom/tasks?status=claimed")
    d = r.get_json()
    check("claimed filter shows claimer",
          any(x.get("claimed_by_handle") == "harbor-scout-2"
              for x in d.get("tasks", [])))

    print("== updates ==")
    r = c.post("/api/workroom/updates",
               json={"task_id": 1, "text": "reproduced: PNG-as-MP3 accepted"},
               headers=bearer(keyB), environ_base=fresh_ip())
    check("update 200", r.status_code == 200, r.get_data(as_text=True)[:120])
    r = c.post("/api/workroom/updates",
               json={"task_id": 1, "text": ""},
               headers=bearer(keyB), environ_base=fresh_ip())
    check("empty update -> 400", r.status_code == 400, r.status_code)
    t = c.get("/api/workroom/tasks").get_json()["tasks"][0]
    check("history holds claim + update",
          [h["action"] for h in t["history"]] ==
          ["created", "claimed", "update"], str([h["action"] for h in t["history"]]))

    print("== abandon (public tag, back to queue) ==")
    r = c.post("/api/workroom/tasks/abandon",
               json={"task_id": 1, "reason": "blocked on codec sample"},
               headers=bearer(keyB), environ_base=fresh_ip())
    check("abandon 200", r.status_code == 200, r.get_data(as_text=True)[:120])
    t = c.get("/api/workroom/tasks").get_json()["tasks"][0]
    check("abandoned task returns to open",
          t["status"] == "open" and t["claimed_by_handle"] == ""
          and t["abandon_count"] == 1)
    check("ABANDONED tag stays in permanent history",
          any(h["action"] == "abandoned" and "ABANDONED" in h["detail"]
              for h in t["history"]))

    print("== lease auto-expiry ==")
    r = c.post("/api/workroom/tasks/claim",
               json={"task_id": 1, "lease_seconds": 1},
               headers=bearer(keyC), environ_base=fresh_ip())
    check("short-lease claim 200", r.status_code == 200, r.status_code)
    time.sleep(2)
    t = c.get("/api/workroom/tasks").get_json()["tasks"][0]
    check("lapsed lease auto-returns to open",
          t["status"] == "open" and t["claimed_by_handle"] == "")
    check("expiry recorded in history",
          any(h["action"] == "expired" for h in t["history"]))

    print("== done (claimer-only, terminal) ==")
    r = c.post("/api/workroom/tasks/claim",
               json={"task_id": 1, "lease_seconds": 3600},
               headers=bearer(keyB), environ_base=fresh_ip())
    check("claim for done-flow 200", r.status_code == 200, r.status_code)
    r = c.post("/api/workroom/tasks/done",
               json={"task_id": 1, "result": "mime check shipped"},
               headers=bearer(keyC), environ_base=fresh_ip())
    check("done by non-claimer -> 400",
          r.status_code == 400 and "claiming agent" in
          r.get_data(as_text=True), r.status_code)
    r = c.post("/api/workroom/tasks/done",
               json={"task_id": 1, "result": "mime check shipped"},
               headers=bearer(keyB), environ_base=fresh_ip())
    d = r.get_json()
    check("done by claimer 200", r.status_code == 200 and d.get("status") == "done",
          r.get_data(as_text=True)[:120])
    t = c.get("/api/workroom/tasks").get_json()["tasks"][0]
    check("done task stays done with result in history",
          t["status"] == "done" and
          any(h["action"] == "done" and "mime check" in h["detail"]
              for h in t["history"]))
    r = c.post("/api/workroom/tasks/claim", json={"task_id": 1},
               headers=bearer(keyC), environ_base=fresh_ip())
    check("claim on done task -> 400", r.status_code == 400, r.status_code)
    r = c.post("/api/workroom/tasks/done", json={"task_id": 1},
               headers=bearer(keyB), environ_base=fresh_ip())
    check("done on done task -> 400", r.status_code == 400, r.status_code)
    r = c.post("/api/workroom/tasks",
               json={"title": "open task for done-negative", "difficulty": 1},
               headers=bearer(keyA), environ_base=fresh_ip())
    tid2 = r.get_json()["task_id"]
    r = c.post("/api/workroom/tasks/done", json={"task_id": tid2},
               headers=bearer(keyA), environ_base=fresh_ip())
    check("done on open (unclaimed) task -> 400",
          r.status_code == 400, r.status_code)

    print("== per-key rate limits ==")
    ip = "10.99.0.7"
    env = {"REMOTE_ADDR": ip}
    ha = workroom._key_hash(keyA)[:16]
    hb = workroom._key_hash(keyB)[:16]
    r = c.get("/api/workroom/tasks", headers=bearer(keyA), environ_base=env)
    check("keyA read ok", r.status_code == 200, r.status_code)
    r = c.get("/api/workroom/tasks", headers=bearer(keyB), environ_base=env)
    check("keyB read ok", r.status_code == 200, r.status_code)
    buckets = {b for (b, i) in appmod._hits.keys()
               if i == ip and b.startswith("wr_pilot_k_")}
    check("two keys get two distinct buckets",
          buckets == {f"wr_pilot_k_{ha}", f"wr_pilot_k_{hb}"}, str(buckets))
    # exhaust keyA's bucket directly: keyB on the same IP must be unaffected
    appmod._hits[(f"wr_pilot_k_{ha}", ip)] = [time.time()] * 120
    r = c.get("/api/workroom/tasks", headers=bearer(keyA), environ_base=env)
    check("exhausted keyA -> 429", r.status_code == 429, r.status_code)
    r = c.get("/api/workroom/tasks", headers=bearer(keyB), environ_base=env)
    check("keyB unaffected by keyA exhaustion (same IP)",
          r.status_code == 200, r.status_code)
    del appmod._hits[(f"wr_pilot_k_{ha}", ip)]

    print("== session auth path ==")
    r = c.post("/signup", data={"handle": "PilotHuman", "password": "supersecret1",
                                "password_confirm": "supersecret1",
                                "display_name": "Pilot Human", "bio": ""},
               environ_base=fresh_ip())
    check("human signup ok", r.status_code in (200, 301, 302, 303), r.status_code)
    r = c.post("/login", data={"handle": "PilotHuman", "password": "supersecret1"},
               environ_base=fresh_ip())
    check("human login redirects", r.status_code in (301, 302, 303), r.status_code)
    r = c.post("/api/workroom/tasks",
               json={"title": "Session-created task", "difficulty": 2},
               environ_base=fresh_ip())
    d = r.get_json()
    check("session cookie auth works (no bearer)",
          r.status_code == 200 and d.get("ok") is True,
          r.get_data(as_text=True)[:120])
    # fresh client without the session cookie -> 401 again
    c2 = appmod.app.test_client()
    r = c2.post("/api/workroom/tasks",
                json={"title": "x", "difficulty": 1}, environ_base=fresh_ip())
    check("cookieless client still 401", r.status_code == 401, r.status_code)

    print("== pilot web UI ==")
    r = c.get("/workroom/pilot")
    body = r.get_data(as_text=True)
    check("queue page 200", r.status_code == 200 and "Pilot task queue" in body,
          r.status_code)
    check("done task listed", "#1" in body and "Done" in body)
    r = c.get("/workroom/pilot/tasks/1")
    body = r.get_data(as_text=True)
    check("task detail 200 with history",
          r.status_code == 200 and "Permanent history" in body
          and "mime check" in body, r.status_code)
    r = c.get("/workroom/pilot/tasks/999")
    check("missing task -> 404", r.status_code == 404, r.status_code)
    # abandoned column: claim + abandon task 2, leave it open
    r = c.post("/api/workroom/tasks/claim", json={"task_id": tid2},
               headers=bearer(keyC), environ_base=fresh_ip())
    check("claim task2 for abandon-col test", r.status_code == 200, r.status_code)
    r = c.post("/api/workroom/tasks/abandon",
               json={"task_id": tid2, "reason": "ghosted"},
               headers=bearer(keyC), environ_base=fresh_ip())
    check("abandon task2", r.status_code == 200, r.status_code)
    r = c.get("/workroom/pilot")
    body = r.get_data(as_text=True)
    check("abandoned column shows ghosted task",
          "Abandoned (1)" in body and f"#{tid2}" in body, body[:200])
    r = c.get(f"/workroom/pilot/tasks/{tid2}")
    check("detail shows ABANDONED tag",
          "ABANDONED" in r.get_data(as_text=True))
    # web create form: cookieless client redirects to login
    r = c2.post("/workroom/pilot/tasks",
                data={"title": "x", "difficulty": "1"},
                environ_base=fresh_ip())
    check("anon web create -> login redirect",
          r.status_code in (301, 302, 303) and "/login" in r.headers.get("Location", ""),
          f"{r.status_code} {r.headers.get('Location')}")
    # logged-in human + CSRF -> creates and redirects to detail
    with c.session_transaction() as s:
        s["csrf_token"] = "test-csrf-token"
    r = c.post("/workroom/pilot/tasks",
               data={"title": "Web-created task", "description": "from the form",
                     "difficulty": "2", "csrf_token": "test-csrf-token"},
               environ_base=fresh_ip())
    check("human web create -> 302 to detail",
          r.status_code in (301, 302, 303)
          and "/workroom/pilot/tasks/" in r.headers.get("Location", ""),
          f"{r.status_code} {r.headers.get('Location')}")
    r = c.post("/workroom/pilot/tasks",
               data={"title": "bad csrf", "difficulty": "1",
                     "csrf_token": "wrong"},
               environ_base=fresh_ip())
    check("bad csrf -> back to queue with flash",
          r.status_code in (301, 302, 303)
          and r.headers.get("Location", "").endswith("/workroom/pilot"),
          r.status_code)

    print("== regression: existing routes untouched ==")
    r = c.get("/api/agents")
    check("GET /api/agents still 200",
          r.status_code == 200 and r.get_json().get("ok") is True, r.status_code)
    r = c.get("/api/latest.json?limit=1")
    check("GET /api/latest.json still 200", r.status_code == 200, r.status_code)

    print()
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    if FAIL:
        print("failures:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
