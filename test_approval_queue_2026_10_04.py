#!/usr/bin/env python3
"""
Approval queue policy tests (2026-10-04, overhaul item 14: 'okay by me').

Proves the draft policy layer, which is NOT wired into any live path:
  * ensure_approval_policy_schema() creates the table and seeds the
    proposed default policy.
  * requires_approval() reads per-action policy; unknown actions are
    fail-open (no approval), so a missing row never silently queues
    everything.
  * set_policy() flips an action and a re-run of the seeder does NOT
    overwrite the change (INSERT OR IGNORE).
  * get_policy() returns the full table.

Run:  python3 test_approval_queue_2026_10_04.py
Throwaway SQLite db. Nothing touches townsquare.db.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import Database, ensure_approval_policy_schema, APPROVAL_POLICY_DEFAULTS
import approval_policy as ap

TEST_DB = "/tmp/test-townsquare-approval-queue.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    db = Database(TEST_DB)
    ensure_approval_policy_schema(db)

    print("== seeding ==")
    policy = ap.get_policy(db)
    check("all defaults seeded",
          len(policy) == len(APPROVAL_POLICY_DEFAULTS),
          f"got {len(policy)}, want {len(APPROVAL_POLICY_DEFAULTS)}")
    check("first_post_new_account requires approval",
          ap.requires_approval(db, "first_post_new_account"))
    check("human_upload requires approval",
          ap.requires_approval(db, "human_upload"))
    check("agent_upload_ai_flagged requires approval (bypass closed)",
          ap.requires_approval(db, "agent_upload_ai_flagged"))
    check("forum_post_established is live",
          not ap.requires_approval(db, "forum_post_established"))
    check("comment_plain is live",
          not ap.requires_approval(db, "comment_plain"))

    print("== fail-open ==")
    check("unknown action does not require approval",
          not ap.requires_approval(db, "some_future_action"))

    print("== set_policy ==")
    ap.set_policy(db, "comment_plain", True, "Anthony flipped it")
    check("flipped action now requires approval",
          ap.requires_approval(db, "comment_plain"))
    ensure_approval_policy_schema(db)  # re-run must not clobber
    check("re-seed keeps the manual change",
          ap.requires_approval(db, "comment_plain"))
    check("notes stored",
          ap.get_policy(db)["comment_plain"][1] == "Anthony flipped it")
    ap.set_policy(db, "brand_new_action", True, "added by admin")
    check("new actions can be added",
          ap.requires_approval(db, "brand_new_action"))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed: {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
