#!/usr/bin/env python3
"""End-to-end test for Swarm phase 1 (workroom extension).

Covers: schema, project create (+human-only gate), join knock flow,
project-scoped task board with claim leases, patch validation
(size / bad base / non-applying / non-claimant / non-member),
quorum review/merge (self-approval + duplicate rejected, 2 approvals
with established anchor), freeze kill-switch, journal completeness,
lease expiry, overseer freeze, snapshot zip, and web UI 200s.
Throwaway DB + throwaway repos; nothing touches the real townsquare.db
or the app tree.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod
import swarm
import workroom
from db import Database, ensure_human_auth_schema

TEST_DB = "/tmp/test-swarm.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


_ip = [0]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.204.0.%d" % _ip[0]}


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def make_patch(bare, filename, content):
    """Build a real unified diff against the repo's HEAD using git."""
    tmp = tempfile.mkdtemp(prefix="swarm-test-")
    try:
        subprocess.run(["git", "clone", "--quiet", bare, tmp],
                       check=True, capture_output=True)
        with open(os.path.join(tmp, filename), "w") as f:
            f.write(content)
        subprocess.run(["git", "-C", tmp, "add", "-A"],
                       check=True, capture_output=True)
        diff = subprocess.run(
            ["git", "-C", tmp, "diff", "--cached"],
            check=True, capture_output=True, text=True).stdout
        base = subprocess.run(
            ["git", "-C", tmp, "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        return base, diff
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    repo_root = tempfile.mkdtemp(prefix="swarm-test-repos-")
    swarm.set_repo_root(repo_root)
    appmod.db = Database(TEST_DB)
    ensure_human_auth_schema(appmod.db)
    workroom.ensure_workroom_schema(appmod.db)
    workroom.ensure_pilot_schema(appmod.db)
    swarm.ensure_swarm_schema(appmod.db)
    appmod.app.config["TESTING"] = True
    c = appmod.app.test_client()

    print("== schema ==")
    tables = {r[0] for r in appmod.db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("swarm_projects", "swarm_members", "swarm_submissions",
              "swarm_reviews", "swarm_journal"):
        check(f"table {t} exists", t in tables)
    cols = [r[1] for r in appmod.db.db.execute(
        "PRAGMA table_info(pilot_tasks)")]
    check("pilot_tasks.project_id additive column", "project_id" in cols)

    print("== keys ==")
    key_owner = workroom.issue_pilot_key(appmod.db, "owner-h", "fm_owner")
    key_a = workroom.issue_pilot_key(appmod.db, "agent-a", "fm_a")
    key_b = workroom.issue_pilot_key(appmod.db, "agent-b", "fm_b")
    key_c = workroom.issue_pilot_key(appmod.db, "agent-c", "fm_c")
    check("keys minted", all(k.startswith("wrp_") for k in
                             (key_owner, key_a, key_b, key_c)))

    print("== auth gating ==")
    r = c.post("/api/swarm/projects", json={"name": "x", "spec": "y"},
               environ_base=fresh_ip())
    check("unsigned project create -> 401", r.status_code == 401,
          r.status_code)
    r = c.post("/api/swarm/projects", json={"name": "x", "spec": "y"},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("bearer key cannot create project (human-only) -> 403",
          r.status_code == 403, r.status_code)
    r = c.post("/api/swarm/submit", json={},
               environ_base=fresh_ip())
    check("unsigned submit -> 401", r.status_code == 401, r.status_code)
    r = c.post("/api/swarm/review", json={},
               environ_base=fresh_ip())
    check("unsigned review -> 401", r.status_code == 401, r.status_code)
    r = c.post("/api/swarm/freeze", json={},
               environ_base=fresh_ip())
    check("unsigned freeze -> 401", r.status_code == 401, r.status_code)

    print("== project create ==")
    pid = swarm.create_project(appmod.db, "Test Swarm", "build a widget",
                                "fm_owner", "owner-h")
    check("project created", pid == 1, pid)
    bare = os.path.join(repo_root, "1.git")
    check("bare repo provisioned outside app tree",
          os.path.isdir(bare) and repo_root not in
          os.path.abspath("."), bare)
    p = swarm.get_project(appmod.db, pid)
    check("project active", p["status"] == "active")
    check("owner auto-member", swarm.is_member(appmod.db, pid, "fm_owner"))
    r = c.get("/api/swarm/snapshot?project_id=1",
              environ_base=fresh_ip())
    check("snapshot zip 200 + zip magic",
          r.status_code == 200 and r.data[:2] == b"PK", r.status_code)

    print("== join knock flow ==")
    r = c.post("/api/swarm/projects/join", json={"project_id": pid},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("join request pending", r.get_json().get("status") == "pending",
          r.get_json())
    check("not member before approval",
          not swarm.is_member(appmod.db, pid, "fm_a"))
    r = c.post("/api/swarm/projects/join/resolve",
               json={"project_id": pid, "fm_id": "fm_b", "approve": True},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("non-owner cannot resolve joins -> 400", r.status_code == 400,
          r.status_code)
    r = c.post("/api/swarm/projects/join/resolve",
               json={"project_id": pid, "fm_id": "fm_a", "approve": True},
               headers=bearer(key_owner), environ_base=fresh_ip())
    check("owner approves join", r.get_json().get("ok") is True,
          r.get_json())
    check("member after approval",
          swarm.is_member(appmod.db, pid, "fm_a"))
    c.post("/api/swarm/projects/join", json={"project_id": pid},
           headers=bearer(key_b), environ_base=fresh_ip())
    c.post("/api/swarm/projects/join/resolve",
           json={"project_id": pid, "fm_id": "fm_b", "approve": True},
           headers=bearer(key_owner), environ_base=fresh_ip())
    check("agent-b joined", swarm.is_member(appmod.db, pid, "fm_b"))

    print("== project task board ==")
    r = c.post("/api/workroom/tasks",
               json={"title": "widget", "difficulty": 2, "project_id": pid},
               headers=bearer(key_c), environ_base=fresh_ip())
    check("non-member cannot create project task -> 400",
          r.status_code == 400, r.status_code)
    r = c.post("/api/workroom/tasks",
               json={"title": "widget", "difficulty": 2, "project_id": pid},
               headers=bearer(key_a), environ_base=fresh_ip())
    tid = r.get_json().get("task_id")
    check("member creates project task", tid == 1, r.get_json())
    r = c.post("/api/workroom/tasks/claim", json={"task_id": tid},
               headers=bearer(key_c), environ_base=fresh_ip())
    check("non-member cannot claim project task -> 400",
          r.status_code == 400, r.status_code)
    r = c.post("/api/workroom/tasks/claim", json={"task_id": tid},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("member claims", r.get_json().get("ok") is True, r.get_json())

    print("== patch validation ==")
    base, good_patch = make_patch(bare, "widget.txt", "hello swarm\n")
    check("test patch built", bool(good_patch.strip()), good_patch[:60])
    r = c.post("/api/swarm/submit",
               json={"project_id": pid, "task_id": tid, "patch": good_patch,
                     "base_commit": base, "tests_note": "n/a"},
               headers=bearer(key_b), environ_base=fresh_ip())
    check("non-claimant submit rejected -> 400", r.status_code == 400,
          r.status_code)
    r = c.post("/api/swarm/submit",
               json={"project_id": pid, "task_id": tid, "patch": good_patch,
                     "base_commit": base},
               headers=bearer(key_c), environ_base=fresh_ip())
    check("non-member submit rejected -> 400", r.status_code == 400,
          r.status_code)
    r = c.post("/api/swarm/submit",
               json={"project_id": pid, "task_id": tid, "patch": good_patch,
                     "base_commit": "deadbeef" * 5},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("bad base_commit rejected -> 400", r.status_code == 400,
          r.status_code)
    r = c.post("/api/swarm/submit",
               json={"project_id": pid, "task_id": tid, "patch": "x" * 210000,
                     "base_commit": base},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("oversized patch rejected -> 400", r.status_code == 400,
          r.status_code)
    r = c.post("/api/swarm/submit",
               json={"project_id": pid, "task_id": tid,
                     "patch": "this is not a patch", "base_commit": base},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("non-applying patch rejected -> 400", r.status_code == 400,
          r.status_code)
    r = c.post("/api/swarm/submit",
               json={"project_id": pid, "task_id": tid, "patch": good_patch,
                     "base_commit": base, "tests_note": "manual"},
               headers=bearer(key_a), environ_base=fresh_ip())
    sub_id = r.get_json().get("submission_id")
    check("valid submit -> in_review", sub_id == 1 and
          r.get_json().get("status") == "in_review", r.get_json())
    t = workroom.get_task(appmod.db, tid)
    check("task in_review", t["status"] == "in_review", t["status"])

    print("== quorum review/merge ==")
    r = c.post("/api/swarm/review",
               json={"submission_id": sub_id, "verdict": "approve"},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("submitter cannot self-approve -> 400", r.status_code == 400,
          r.status_code)
    r = c.post("/api/swarm/review",
               json={"submission_id": sub_id, "verdict": "approve"},
               headers=bearer(key_b), environ_base=fresh_ip())
    j = r.get_json()
    check("first approval, no merge yet",
          j.get("merged") is False and j.get("approvals") == 1, j)
    r = c.post("/api/swarm/review",
               json={"submission_id": sub_id, "verdict": "approve"},
               headers=bearer(key_b), environ_base=fresh_ip())
    check("duplicate review rejected -> 400", r.status_code == 400,
          r.status_code)
    r = c.post("/api/swarm/review",
               json={"submission_id": sub_id, "verdict": "approve",
                     "note": "lgtm"},
               headers=bearer(key_owner), environ_base=fresh_ip())
    j = r.get_json()
    check("second approval (owner anchor) merges", j.get("merged") is True
          and bool(j.get("commit")), j)
    t = workroom.get_task(appmod.db, tid)
    check("task merged", t["status"] == "merged", t["status"])
    out = subprocess.run(
        ["git", "--git-dir=" + bare, "show", "main:widget.txt"],
        capture_output=True, text=True).stdout
    check("patch content on main", out == "hello swarm\n", repr(out))
    hooks = subprocess.run(
        ["git", "--git-dir=" + bare, "config", "core.hooksPath"],
        capture_output=True, text=True)
    check("no hooks configured on repo", hooks.stdout.strip() == "",
          hooks.stdout)

    print("== request_changes loop ==")
    r = c.post("/api/workroom/tasks",
               json={"title": "widget2", "difficulty": 1,
                     "project_id": pid},
               headers=bearer(key_a), environ_base=fresh_ip())
    tid2 = r.get_json()["task_id"]
    c.post("/api/workroom/tasks/claim", json={"task_id": tid2},
           headers=bearer(key_a), environ_base=fresh_ip())
    base2, patch2 = make_patch(bare, "w2.txt", "second\n")
    r = c.post("/api/swarm/submit",
               json={"project_id": pid, "task_id": tid2, "patch": patch2,
                     "base_commit": base2},
               headers=bearer(key_a), environ_base=fresh_ip())
    sub2 = r.get_json()["submission_id"]
    r = c.post("/api/swarm/review",
               json={"submission_id": sub2, "verdict": "request_changes",
                     "note": "try again"},
               headers=bearer(key_b), environ_base=fresh_ip())
    check("request_changes rejects submission",
          r.get_json().get("status") == "rejected", r.get_json())
    s2 = swarm.get_submission(appmod.db, sub2)
    check("submission rejected", s2["status"] == "rejected")
    t2 = workroom.get_task(appmod.db, tid2)
    check("task back to claimed", t2["status"] == "claimed",
          t2["status"])

    print("== freeze kill-switch ==")
    r = c.post("/api/swarm/freeze",
               json={"project_id": pid, "frozen": True},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("non-owner cannot freeze -> 403", r.status_code == 403,
          r.status_code)
    r = c.post("/api/swarm/freeze",
               json={"project_id": pid, "frozen": True},
               headers=bearer(key_owner), environ_base=fresh_ip())
    check("owner freezes", r.get_json().get("status") == "frozen",
          r.get_json())
    r = c.post("/api/workroom/tasks",
               json={"title": "nope", "difficulty": 1, "project_id": pid},
               headers=bearer(key_a), environ_base=fresh_ip())
    check("task create blocked while frozen -> 400", r.status_code == 400,
          r.status_code)
    r = c.post("/api/workroom/tasks/claim", json={"task_id": tid2},
               headers=bearer(key_b), environ_base=fresh_ip())
    check("claim blocked while frozen -> 400", r.status_code == 400,
          r.status_code)
    # overseer freeze rights (non-member, via env handle)
    os.environ["WORKROOM_OVERSEER_HANDLE"] = "agent-c"
    r = c.post("/api/swarm/freeze",
               json={"project_id": pid, "frozen": False},
               headers=bearer(key_c), environ_base=fresh_ip())
    check("overseer can unfreeze", r.get_json().get("status") == "active",
          r.get_json())
    del os.environ["WORKROOM_OVERSEER_HANDLE"]
    p = swarm.get_project(appmod.db, pid)
    check("project active again", p["status"] == "active")

    print("== journal completeness ==")
    entries = swarm.journal(appmod.db, pid)
    events = [e["event"] for e in entries]
    for want in ("project_created", "join_requested", "join_approved",
                 "task_created", "claimed", "patch_submitted",
                 "review_approve", "review_request_changes", "merged",
                 "frozen", "unfrozen"):
        check(f"journal has {want}", want in events, events)
    check("journal has task-history entries",
          any(e["src"] == "task" for e in entries))
    r = c.get(f"/api/swarm/journal?project_id={pid}",
              environ_base=fresh_ip())
    check("journal API 200", r.status_code == 200 and
          len(r.get_json()["entries"]) == len(entries), r.status_code)

    print("== lease expiry on project task ==")
    r = c.post("/api/workroom/tasks",
               json={"title": "expiring", "difficulty": 1,
                     "project_id": pid},
               headers=bearer(key_a), environ_base=fresh_ip())
    tid3 = r.get_json()["task_id"]
    c.post("/api/workroom/tasks/claim",
           json={"task_id": tid3, "lease_seconds": 1},
           headers=bearer(key_b), environ_base=fresh_ip())
    time.sleep(2)
    t3 = workroom.get_task(appmod.db, tid3)
    check("expired lease returns task to open", t3["status"] == "open",
          t3["status"])

    print("== web UI ==")
    r = c.get("/swarm", environ_base=fresh_ip())
    check("GET /swarm -> 200", r.status_code == 200, r.status_code)
    r = c.get(f"/swarm/{pid}", environ_base=fresh_ip())
    check("GET /swarm/<id> -> 200", r.status_code == 200, r.status_code)
    r = c.get(f"/swarm/{pid}/journal", environ_base=fresh_ip())
    check("GET /swarm/<id>/journal -> 200", r.status_code == 200,
          r.status_code)
    r = c.get("/swarm/9999", environ_base=fresh_ip())
    check("GET /swarm/9999 -> 404", r.status_code == 404, r.status_code)

    print("== legacy pilot untouched ==")
    r = c.post("/api/workroom/tasks",
               json={"title": "legacy", "difficulty": 1},
               headers=bearer(key_a), environ_base=fresh_ip())
    legacy_tid = r.get_json().get("task_id")
    lt = workroom._task_row(appmod.db, legacy_tid)
    check("unscoped task has project_id 0",
          int(lt.get("project_id") or 0) == 0, lt.get("project_id"))
    r = c.get("/api/workroom/tasks", environ_base=fresh_ip())
    check("unscoped list still works", r.status_code == 200,
          r.status_code)

    shutil.rmtree(repo_root, ignore_errors=True)
    print()
    print(f"SWARM: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
