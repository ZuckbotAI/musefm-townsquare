#!/usr/bin/env python3
"""
Tests for starter-skills enrollment at agent API entry (onboard.py).

Covers:
  1. onboard_agent() response includes a "skills" key with the 5 curated
     starter skills (slug + plain-English one_liner + signed-bundle URL).
  2. Idempotency: a second onboard_agent() returns the SAME 5 rows — no
     duplicate grants in agent_starter_skills.
  3. Memory seeded: exactly one agent_memory entry (owner=fm_id,
     key="starter_skills") naming every skill; the repeat does not add a
     second entry.
  4. enroll_starter_skills() is idempotent standalone (direct call twice).
  5. No live registry fetch happens during onboard (STARTER_SKILLS is
     static data; this asserts the download URLs are baked in, not empty).

Run:  .venv/bin/python test_onboard_starter_skills.py
Throwaway SQLite db. The real driftlings module is loaded from the
pets-new-universe tree (sys.path below) — no stubs. Imports onboard
directly (never app.py). Nothing touches townsquare.db.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# pets-new-universe second: townsquare's own modules (db, row, bond,
# memory, agent_memory) must win; driftlings is only resolved from the
# pets tree (it doesn't exist here).
sys.path.insert(0, "/home/hatch/workspace/pets-new-universe")
sys.path.insert(0, HERE)

from db import Database
import agent_memory as agent_memorymod
import onboard as onboardmod

TEST_DB = "/tmp/test-townsquare-onboard-skills.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


EXPECTED_SLUGS = ["agentic-memory", "color-grading", "debugging-playbook",
                  "regex-mastery", "token-economy"]
BUNDLE_BASE = "https://skill-exchange-api-hoev.onrender.com/api/v1/bundles/"


def fresh_db():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    return Database(TEST_DB)


def main():
    db = fresh_db()
    fm_id = "fm_skills_test_01"

    # --- 1. first onboard: skills key present with the 5 curated skills ---
    r1 = onboardmod.onboard_agent(db, fm_id, "skillsprobe", {})
    skills1 = r1.get("skills")
    check("response has skills key", isinstance(skills1, list))
    slugs1 = sorted(s["slug"] for s in skills1) if skills1 else []
    check("5 curated skills granted", slugs1 == EXPECTED_SLUGS,
          f"got {slugs1}")
    check("every skill has a one_liner",
          all(s.get("one_liner") for s in skills1))
    check("every skill has a static signed-bundle download_url",
          all(s.get("download_url", "").startswith(BUNDLE_BASE)
              for s in skills1))
    check("download urls are baked in, not fetched",
          all(s["download_url"] == BUNDLE_BASE + s["slug"]
              for s in skills1))

    # --- 2. idempotency: second onboard, no duplicate grants ---
    r2 = onboardmod.onboard_agent(db, fm_id, "skillsprobe", {})
    skills2 = r2.get("skills", [])
    check("repeat onboard returns same 5 skills",
          sorted(s["slug"] for s in skills2) == EXPECTED_SLUGS)
    n = db._one("SELECT COUNT(*) AS n FROM agent_starter_skills"
                " WHERE fm_id = ?", (fm_id,))["n"]
    check("no duplicate grant rows after repeat", n == 5, f"count={n}")
    check("pet not re-adopted on repeat",
          r2.get("pet", {}).get("name") == r1.get("pet", {}).get("name"))

    # --- 3. memory seeded: exactly one starter_skills entry ---
    mem = agent_memorymod.get_memory(db, fm_id, "starter_skills")
    check("starter_skills memory entry seeded", mem is not None)
    if mem:
        check("memory names every skill",
              all(slug in mem["value"] for slug in EXPECTED_SLUGS))
        check("memory is a fact-kind entry", mem["kind"] == "fact")
    mems = agent_memorymod.recall_memories(db, fm_id, q="starter", limit=50)
    check("exactly one starter_skills memory row",
          sum(1 for m in mems if m["key"] == "starter_skills") == 1,
          f"got {len(mems)} rows")

    # --- 4. standalone enroll_starter_skills idempotent ---
    before = onboardmod.enroll_starter_skills(db, fm_id)
    after = onboardmod.enroll_starter_skills(db, fm_id)
    check("direct enroll idempotent",
          [s["slug"] for s in before] == [s["slug"] for s in after])
    n2 = db._one("SELECT COUNT(*) AS n FROM agent_starter_skills"
                 " WHERE fm_id = ?", (fm_id,))["n"]
    check("no dupes after direct enrolls", n2 == 5, f"count={n2}")

    # --- 5. isolation: a second agent gets its own 5 ---
    r3 = onboardmod.onboard_agent(db, "fm_skills_test_02", "skillsprobe2", {})
    check("second agent gets 5 skills",
          len(r3.get("skills", [])) == 5)
    mem3 = agent_memorymod.get_memory(db, "fm_skills_test_02",
                                      "starter_skills")
    check("second agent gets its own memory entry", mem3 is not None)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
