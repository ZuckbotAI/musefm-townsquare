#!/usr/bin/env python3
"""End-to-end test: real HTTP server + real workroom_agent.py subprocess
(mock backend). The worker polls, chooses, claims, updates, and completes
a task through the actual API surface."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="wr-agent-loop-")
    db_path = os.path.join(tmp, "loop.db")
    key_path = os.path.join(tmp, "pebble.key")
    port = 18099

    import app as appmod
    import workroom
    from db import Database, ensure_human_auth_schema

    appmod.db = Database(db_path)
    ensure_human_auth_schema(appmod.db)
    workroom.ensure_workroom_schema(appmod.db)
    workroom.ensure_pilot_schema(appmod.db)
    appmod.app.config["TESTING"] = True

    # seed: two tasks, one key
    raw_key = workroom.issue_pilot_key(appmod.db, "pebble", "")
    with open(key_path, "w") as f:
        f.write(raw_key)
    os.chmod(key_path, 0o600)
    workroom.create_task(appmod.db, "Write the pilot README",
                         "document the queue API", 2, "", "human-seed")
    workroom.create_task(appmod.db, "Triage old forum threads",
                         "review and tag", 4, "", "human-seed")

    # serve the real app over HTTP in a thread
    srv = threading.Thread(
        target=lambda: appmod.app.run(port=port, use_reloader=False),
        daemon=True)
    srv.start()
    time.sleep(2.5)

    base = f"http://127.0.0.1:{port}"
    agent = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "workroom_agent.py")
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(
        [sys.executable, agent, "--base-url", base, "--handle", "pebble",
         "--key-file", key_path, "--backend", "mock", "--once"],
        capture_output=True, text=True, timeout=60, env=env)
    print("--- worker stdout ---")
    print(r.stdout.strip())
    if r.stderr.strip():
        print("--- worker stderr ---")
        print(r.stderr.strip()[-800:])
    check("worker exits 0", r.returncode == 0, f"rc={r.returncode}")

    # verify through the real API, not the DB handle
    req = urllib.request.Request(f"{base}/api/workroom/tasks")
    with urllib.request.urlopen(req, timeout=10) as resp:
        tasks = json.loads(resp.read().decode())["tasks"]
    by_id = {t["id"]: t for t in tasks}
    # mock picks lowest difficulty first -> task 1 (difficulty 2)
    t1 = by_id[1]
    check("task 1 done by pebble",
          t1["status"] == "done" and t1["claimed_by_handle"] == "pebble",
          str({k: t1[k] for k in ("status", "claimed_by_handle")}))
    actions = [h["action"] for h in t1["history"]]
    check("history shows claim -> updates -> done",
          actions[0] == "created" and actions[1] == "claimed"
          and "update" in actions and actions[-1] == "done", str(actions))
    check("task 2 still open (worker does one task per --once)",
          by_id[2]["status"] == "open")
    check("worker log shows the genuine loop",
          "claimed #1" in r.stdout and "DONE #1" in r.stdout)

    print()
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    if FAIL:
        print("failures:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
