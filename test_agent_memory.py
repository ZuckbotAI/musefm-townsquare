#!/usr/bin/env python3
"""Tests for the Agentic Memory API (agent_memory.py + app.py routes).

Module tests run against a temp Database; route tests run against a real
HTTP server with real pilot Bearer <redacted> (same pattern as
test_workroom_agent_loop.py).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def expect_value_error(name, fn):
    try:
        fn()
    except ValueError:
        check(name, True)
    except Exception as e:
        check(name, False, f"wrong exception: {type(e).__name__}: {e}")
    else:
        check(name, False, "no exception raised")


def fresh_db():
    import app as appmod
    from db import Database, ensure_human_auth_schema
    tmp = tempfile.mkdtemp(prefix="amem-")
    db_path = os.path.join(tmp, "amem.db")
    appmod.db = Database(db_path)
    ensure_human_auth_schema(appmod.db)
    import workroom
    import agent_memory
    workroom.ensure_workroom_schema(appmod.db)
    workroom.ensure_pilot_schema(appmod.db)
    agent_memory.ensure_agent_memory_schema(appmod.db)
    return appmod.db


def test_module():
    import agent_memory
    db = fresh_db()

    # 1. store + get round trip
    m = agent_memory.store_memory(db, "pebble", "fact", "user-tz",
                                  "user is in America/Chicago", 0.9)
    check("store round trip", m["owner"] == "pebble" and
          m["kind"] == "fact" and m["key"] == "user-tz" and
          m["value"] == "user is in America/Chicago" and
          m["confidence"] == 0.9)

    # 2. upsert overwrites
    m2 = agent_memory.store_memory(db, "pebble", "fact", "user-tz",
                                   "user moved to Denver", 0.7)
    check("upsert overwrites value", m2["value"] == "user moved to Denver"
          and m2["confidence"] == 0.7 and m2["id"] == m["id"])

    # 3. namespaces are separate
    agent_memory.store_memory(db, "rill", "fact", "user-tz",
                              "rill has no idea", 0.5)
    check("owner namespaces separate",
          agent_memory.get_memory(db, "pebble", "user-tz")["value"] ==
          "user moved to Denver")

    # 4. recall keyword filtering (ALL tokens must match)
    agent_memory.store_memory(db, "pebble", "lesson", "deploy-friday",
                              "never deploy on friday evening", 1.0)
    agent_memory.store_memory(db, "pebble", "outcome", "task-41",
                              "finished task 41 ahead of schedule", 1.0)
    hits = agent_memory.recall_memories(db, "pebble", q="deploy friday")
    check("recall all-tokens-must-match",
          len(hits) == 1 and hits[0]["key"] == "deploy-friday")
    hits = agent_memory.recall_memories(db, "pebble", q="friday deploy")
    check("recall token order irrelevant", len(hits) == 1)
    hits = agent_memory.recall_memories(db, "pebble", q="friday nonexistent")
    check("recall no partial match", hits == [])

    # 5. kind filter + limit
    hits = agent_memory.recall_memories(db, "pebble", q="", kind="lesson")
    check("recall kind filter",
          len(hits) == 1 and hits[0]["kind"] == "lesson")
    hits = agent_memory.recall_memories(db, "pebble", q="", limit=1)
    check("recall limit respected", len(hits) == 1)

    # 6. update value/confidence
    u = agent_memory.update_memory(db, "pebble", "user-tz",
                                   value="user is back in Chicago",
                                   confidence=0.95)
    check("update value+confidence",
          u["value"] == "user is back in Chicago" and u["confidence"] == 0.95)
    try:
        agent_memory.update_memory(db, "pebble", "nope", value="x")
        check("update missing raises", False, "no exception")
    except LookupError:
        check("update missing raises", True)

    # 7. forget
    check("forget returns True",
          agent_memory.forget_memory(db, "pebble", "deploy-friday") is True)
    check("forget removes", agent_memory.get_memory(
        db, "pebble", "deploy-friday") is None)
    try:
        agent_memory.forget_memory(db, "pebble", "deploy-friday")
        check("forget missing raises", False, "no exception")
    except LookupError:
        check("forget missing raises", True)

    # 8. decay: only stale AND low-confidence get pruned
    now = int(time.time())
    old = now - 100 * 86400
    db.db.execute(
        "INSERT INTO agent_memories (owner, kind, key, value, confidence,"
        " created_at, updated_at) VALUES "
        "('pebble','fact','stale-low','old junk',0.2,?,?),"
        "('pebble','fact','stale-high','old gold',0.9,?,?),"
        "('pebble','fact','fresh-low','new junk',0.2,?,?)",
        (old, old, old, old, now, now))
    db.db.commit()
    n = agent_memory.decay_memories(db, "pebble", older_than_days=90,
                                    below_confidence=0.4)
    check("decay prunes stale+low only", n == 1)
    check("decay keeps stale high-confidence",
          agent_memory.get_memory(db, "pebble", "stale-high") is not None)
    check("decay keeps fresh low-confidence",
          agent_memory.get_memory(db, "pebble", "fresh-low") is not None)

    # 9. audit log has entries
    rows = db.db.execute(
        "SELECT COUNT(*) c FROM agent_memory_events"
        " WHERE owner='pebble'").fetchone()
    check("audit log written", rows["c"] >= 7, f"count={rows['c']}")

    # 10. validation
    expect_value_error("bad kind rejected",
                       lambda: agent_memory.store_memory(
                           db, "pebble", "vibe", "k", "v"))
    expect_value_error("bad owner rejected",
                       lambda: agent_memory.store_memory(
                           db, "PEBBLE!!", "fact", "k", "v"))
    expect_value_error("bad confidence rejected",
                       lambda: agent_memory.store_memory(
                           db, "pebble", "fact", "k", "v", 1.5))
    expect_value_error("empty key rejected",
                       lambda: agent_memory.store_memory(
                           db, "pebble", "fact", "", "v"))
    expect_value_error("empty value rejected",
                       lambda: agent_memory.store_memory(
                           db, "pebble", "fact", "k", "   "))
    expect_value_error("oversize value rejected",
                       lambda: agent_memory.store_memory(
                           db, "pebble", "fact", "k", "x" * 4001))
    expect_value_error("nothing-to-update rejected",
                       lambda: agent_memory.update_memory(
                           db, "pebble", "user-tz"))


def _http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {"error": f"http {e.code}"}
        return e.code, payload


def test_routes():
    import app as appmod
    import workroom
    db = fresh_db()
    key_pebble = workroom.issue_pilot_key(db, "pebble", "")
    key_rill = workroom.issue_pilot_key(db, "rill", "")
    port = 18101
    srv = threading.Thread(
        target=lambda: appmod.app.run(port=port, use_reloader=False),
        daemon=True)
    srv.start()
    time.sleep(2.5)
    base = f"http://127.0.0.1:{port}"
    hp = {"Authorization": f"Bearer {key_pebble}"}
    hr = {"Authorization": f"Bearer {key_rill}"}

    # 1. store via API (own namespace)
    c, d = _http("POST", base + "/api/agent-memory/store",
                 {"owner": "pebble", "kind": "preference",
                  "key": "tone", "value": "short replies", "confidence": 0.8},
                 hp)
    check("API store 200", c == 200 and d["memory"]["key"] == "tone",
          f"{c} {d}")

    # 2. shared namespace writable by any agent
    c, d = _http("POST", base + "/api/agent-memory/store",
                 {"owner": "shared", "kind": "lesson",
                  "key": "rate-limits", "value": "respect 429s"}, hp)
    check("API store shared 200", c == 200)

    # 3. cross-agent write forbidden
    c, d = _http("POST", base + "/api/agent-memory/store",
                 {"owner": "rill", "kind": "fact",
                  "key": "x", "value": "y"}, hp)
    check("API cross-agent write 403", c == 403, f"{c} {d}")

    # 4. rill can read shared, not pebble's private
    c, d = _http("GET", base + "/api/agent-memory/recall?owner=shared&q=rate",
                 headers=hr)
    check("API recall shared by other agent",
          c == 200 and len(d["memories"]) == 1, f"{c} {d}")
    c, d = _http("GET", base + "/api/agent-memory/recall?owner=pebble&q=tone",
                 headers=hr)
    check("API cross-agent read 403", c == 403, f"{c} {d}")

    # 5. recall via API (own)
    c, d = _http("GET", base + "/api/agent-memory/recall?owner=pebble&q=short",
                 headers=hp)
    check("API recall 200", c == 200 and len(d["memories"]) == 1,
          f"{c} {d}")

    # 6. update via API
    c, d = _http("PATCH", base + "/api/agent-memory/update",
                 {"owner": "pebble", "key": "tone",
                  "value": "short replies, warm tone"}, hp)
    check("API update 200", c == 200 and
          d["memory"]["value"] == "short replies, warm tone", f"{c} {d}")
    c, d = _http("PATCH", base + "/api/agent-memory/update",
                 {"owner": "pebble", "key": "missing", "value": "x"}, hp)
    check("API update missing 404", c == 404, f"{c} {d}")

    # 7. forget via API
    c, d = _http("DELETE", base + "/api/agent-memory/forget",
                 {"owner": "pebble", "key": "tone"}, hp)
    check("API forget 200", c == 200 and d["ok"] is True, f"{c} {d}")
    c, d = _http("GET", base + "/api/agent-memory/list?owner=pebble",
                 headers=hp)
    check("API list empty after forget",
          c == 200 and d["memories"] == [], f"{c} {d}")

    # 8. decay via API
    c, d = _http("POST", base + "/api/agent-memory/decay",
                 {"owner": "pebble", "older_than_days": 0,
                  "below_confidence": 1.0}, hp)
    check("API decay 200", c == 200 and isinstance(d["pruned"], int),
          f"{c} {d}")

    # 9. unauthenticated
    c, d = _http("POST", base + "/api/agent-memory/store",
                 {"owner": "pebble", "kind": "fact", "key": "x", "value": "y"})
    check("API unauth 401", c == 401, f"{c} {d}")

    # 10. rate limit still applies (per-key bucket shared with pilot)
    codes = set()
    for _ in range(130):
        c, _ = _http("GET", base + "/api/agent-memory/list?owner=pebble",
                     headers=hp)
        codes.add(c)
    check("API rate limit trips", 429 in codes, f"codes={sorted(codes)}")


def main():
    print("== module tests ==")
    test_module()
    print("== route tests ==")
    test_routes()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
