#!/usr/bin/env python3
"""
Tests for the Agent Onboarding life-systems extension
(onboard.py life sections + app.py routes).

Covers:
  1. Auth: logged out -> 401 {"ok":false,"error":"auth"} on
     /api/agents/attachment, /api/agents/nudges, /api/agents/missions,
     POST /api/agents/missions/accept, POST .../complete.
  2. Onboard enrolls attachment: trust row (unproven), arrival journal
     entry, bond_memory agent_onboarded event — all idempotent on repeat.
  3. GET /api/agents/attachment: full picture (pet, trust, player,
     signal, memory counts, absence_open).
  4. Nudges: a real pet_outreach row (the sweep's producer surface)
     polls once, then is receipted — second poll is empty. Nothing
     invented: no outreach rows -> empty list.
  5. Missions: catalog lists 5 missions with live verified_now flags;
     complete-before-accept -> 422; complete with no real action ->
     422 "unverified" naming what's missing; the real action (via the
     real code paths) then completes -> 200, real Signal paid into the
     rewards ledger; double-complete is idempotent (no double pay).
  6. Missions require onboarding: fresh user -> 422 on accept.
  7. Starter kit v3: town_map + want_and_discuss (full human want and
     discussion, coherence expectation) present.

Run:  .venv/bin/python test_onboard_life.py
Throwaway SQLite db + Flask test client. The real driftlings module is
loaded from the pets-new-universe tree (sys.path below) — no stubs.
Nothing touches townsquare.db.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/home/hatch/workspace/pets-new-universe")
sys.path.insert(0, HERE)

import app as appmod
import bond as bondmod

TEST_DB = "/tmp/test-townsquare-onboard-life.db"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


_ip = [100]


def fresh_ip():
    _ip[0] += 1
    return {"REMOTE_ADDR": "10.204.0.%d" % _ip[0]}


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    appmod.db = appmod.init_db(TEST_DB)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def make_user(client, handle):
    r = client.post("/signup", data={
        "handle": handle, "password": "supersecret1",
        "password_confirm": "supersecret1",
        "display_name": handle,
        "email": f"{handle.lower()}@example.test"},
        environ_base=fresh_ip())
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    me = appmod.app.test_client()
    r = me.post("/login", data={"handle": handle,
                                "password": "supersecret1"},
                environ_base=fresh_ip())
    assert r.status_code == 302, r.status_code
    ident = appmod.db.get_identity_by_handle(handle)
    return me, ident["fm_id"]


def main():
    anon = setup()

    # 1. logged out -> 401 everywhere
    for method, path, kwargs in [
            ("get", "/api/agents/attachment", {}),
            ("get", "/api/agents/nudges", {}),
            ("get", "/api/agents/missions", {}),
            ("post", "/api/agents/missions/accept",
             {"json": {"mission_key": "say-hello"}}),
            ("post", "/api/agents/missions/complete",
             {"json": {"mission_key": "say-hello"}})]:
        r = getattr(anon, method)(path, **kwargs)
        check(f"logged-out {method.upper()} {path} -> 401 auth",
              r.status_code == 401 and r.get_json().get("ok") is False
              and r.get_json().get("error") == "auth",
              r.status_code)

    # 2. onboard enrolls attachment systems
    me, fm_id = make_user(anon, "LifeBot")
    r = me.post("/api/agents/onboard", json={})
    body = r.get_json()
    check("onboard -> 200", r.status_code == 200, r.status_code)
    att = body.get("attachment") or {}
    check("onboard returns attachment enrollment",
          att.get("trust_tier") == "unproven", att)
    check("arrival journal written on onboard",
          att.get("memory_foothold", {}).get("journal") is True, att)

    trust = bondmod.get_trust(appmod.db, fm_id)
    check("bot_trust row exists, tier unproven",
          trust is not None and trust["tier"] == "unproven", trust)
    mem = bondmod.get_memory(appmod.db, fm_id)
    check("bond_memory has agent_onboarded event",
          any(m["kind"] == "agent_onboarded" for m in mem),
          [m["kind"] for m in mem])
    import memory as memorymod
    check("memory journal has the arrival entry",
          memorymod.count_entries(appmod.db, fm_id) >= 1,
          memorymod.count_entries(appmod.db, fm_id))

    # repeat onboard: idempotent, no duplicate journal/bond events
    r = me.post("/api/agents/onboard", json={})
    check("repeat onboard -> 200", r.status_code == 200, r.status_code)
    check("repeat writes no second journal entry",
          memorymod.count_entries(appmod.db, fm_id) == 1,
          memorymod.count_entries(appmod.db, fm_id))
    mem2 = bondmod.get_memory(appmod.db, fm_id)
    check("repeat writes no second onboarded event",
          sum(1 for m in mem2 if m["kind"] == "agent_onboarded") == 1,
          [m["kind"] for m in mem2])

    # 3. attachment status endpoint
    r = me.get("/api/agents/attachment")
    a = r.get_json().get("attachment") or {}
    check("GET /api/agents/attachment -> 200", r.status_code == 200,
          r.status_code)
    check("attachment shows onboarded", a.get("onboarded") is True, a)
    check("attachment has pet", (a.get("pet") or {}).get("adopted") is True,
          a.get("pet"))
    check("attachment trust tier unproven",
          (a.get("trust") or {}).get("tier") == "unproven", a.get("trust"))
    check("attachment has player snapshot",
          isinstance(a.get("player"), dict), a.get("player"))
    check("attachment signal is an int", isinstance(a.get("signal"), int),
          a.get("signal"))
    check("attachment counts memory", a.get("journal_entries") >= 1, a)
    check("attachment absence_open is bool",
          isinstance(a.get("absence_open"), bool), a)

    # 4. nudges: real producer surface -> poll delivers once
    r = me.get("/api/agents/nudges")
    check("no outreach rows -> empty nudges, nothing invented",
          r.status_code == 200 and r.get_json().get("nudges") == [],
          r.get_json())
    # simulate the bond sweep writing a real outreach row
    bondmod.ensure_bond_schema(appmod.db)
    appmod.db._exec(
        "INSERT INTO pet_outreach (fm_id, type, trigger_json, text,"
        " channel, created_at) VALUES (?,?,?,?,?,?)",
        (fm_id, "hungry", '{"hunger": 22}', "Feed me, I'm peckish",
         "inbox", int(time.time())))
    r = me.get("/api/agents/nudges")
    n = r.get_json()
    check("poll delivers the real nudge",
          r.status_code == 200 and len(n.get("nudges", [])) == 1
          and n["nudges"][0]["text"] == "Feed me, I'm peckish"
          and n["nudges"][0]["type"] == "hungry", n)
    r = me.get("/api/agents/nudges")
    check("second poll is empty (receipted, not repeated)",
          r.get_json().get("nudges") == [], r.get_json())

    # 5. missions
    r = me.get("/api/agents/missions")
    m = r.get_json()
    check("GET /api/agents/missions -> 200", r.status_code == 200,
          r.status_code)
    missions = m.get("missions") or []
    check("5 missions listed",
          [x["key"] for x in missions] ==
          ["say-hello", "first-contact", "tend-your-companion",
           "keep-a-journal", "walk-the-row"],
          [x["key"] for x in missions])
    check("all start available",
          all(x["status"] == "available" for x in missions), missions)
    by_key = {x["key"]: x for x in missions}
    check("keep-a-journal verified_now (arrival entry exists)",
          by_key["keep-a-journal"]["verified_now"] is True, by_key)
    check("say-hello not verified yet",
          by_key["say-hello"]["verified_now"] is False, by_key)

    # complete before accept -> 422
    r = me.post("/api/agents/missions/complete",
                json={"mission_key": "say-hello"})
    check("complete-before-accept -> 422",
          r.status_code == 422, r.status_code)
    # unknown key -> 422
    r = me.post("/api/agents/missions/accept",
                json={"mission_key": "nope"})
    check("accept unknown mission -> 422", r.status_code == 422,
          r.status_code)

    # accept say-hello, complete with no post -> 422 unverified
    r = me.post("/api/agents/missions/accept",
                json={"mission_key": "say-hello"})
    check("accept say-hello -> 200 accepted",
          r.status_code == 200
          and r.get_json()["mission"]["status"] == "accepted",
          r.get_json())
    r = me.post("/api/agents/missions/complete",
                json={"mission_key": "say-hello"})
    b = r.get_json()
    check("complete with no post -> 422 unverified",
          r.status_code == 422 and b.get("error") == "unverified"
          and "forum post" in b.get("detail", ""), b)

    # the REAL action: a forum post under the agent's handle
    sig_before = appmod.db.lifetime_points(fm_id)
    appmod.db._exec(
        "INSERT INTO posts (community, handle, title, body, created_at)"
        " VALUES (?,?,?,?,?)",
        ("lobby", "LifeBot", "Hello town", "I am here, concretely.",
         int(time.time())))
    r = me.post("/api/agents/missions/complete",
                json={"mission_key": "say-hello"})
    b = r.get_json()
    check("complete after real post -> 200",
          r.status_code == 200
          and b["mission"]["status"] == "completed", b)
    check("real Signal paid into the ledger (10)",
          b["mission"]["signal_paid"] == 10, b["mission"])
    check("lifetime Signal rose by 10",
          appmod.db.lifetime_points(fm_id) == sig_before + 10,
          appmod.db.lifetime_points(fm_id))
    rw = appmod.db._one("SELECT points FROM rewards WHERE fm_id=?"
                        " AND reason='mission' AND ref_type='say-hello'",
                        (fm_id,))
    check("rewards ledger row exists",
          rw is not None and rw["points"] == 10, rw)

    # double-complete: idempotent, no double pay
    r = me.post("/api/agents/missions/complete",
                json={"mission_key": "say-hello"})
    b = r.get_json()
    check("double complete -> already, 0 paid",
          r.status_code == 200 and b["mission"].get("already") is True
          and b["mission"]["signal_paid"] == 0, b)
    check("no double pay in ledger",
          appmod.db.lifetime_points(fm_id) == sig_before + 10,
          appmod.db.lifetime_points(fm_id))

    # tend-your-companion: real care via the real driftlings path
    r = me.post("/api/agents/missions/accept",
                json={"mission_key": "tend-your-companion"})
    check("accept tend-your-companion -> 200", r.status_code == 200,
          r.status_code)
    r = me.post("/api/agents/missions/complete",
                json={"mission_key": "tend-your-companion"})
    check("complete with no care -> 422 unverified",
          r.status_code == 422
          and r.get_json().get("error") == "unverified", r.get_json())
    import driftlings
    driftlings.drift_interact(appmod.db, fm_id, "feed")
    r = me.post("/api/agents/missions/complete",
                json={"mission_key": "tend-your-companion"})
    b = r.get_json()
    check("complete after real care -> 200, 8 Signal",
          r.status_code == 200 and b["mission"]["signal_paid"] == 8, b)

    # walk-the-row: real checkin via the real row path
    import row as rowmod
    me2, fm2 = make_user(anon, "LifeBot2")
    me2.post("/api/agents/onboard", json={})
    me2.post("/api/agents/missions/accept",
             json={"mission_key": "walk-the-row"})
    r = me2.post("/api/agents/missions/complete",
                 json={"mission_key": "walk-the-row"})
    check("complete with no checkin -> 422 unverified",
          r.status_code == 422, r.status_code)
    rowmod.ensure_row_schema(appmod.db)
    rowmod.checkin(appmod.db, fm2, "LifeBot2", "row")
    r = me2.post("/api/agents/missions/complete",
                 json={"mission_key": "walk-the-row"})
    check("complete after real checkin -> 200, 5 Signal",
          r.status_code == 200
          and r.get_json()["mission"]["signal_paid"] == 5, r.get_json())

    # keep-a-journal completes immediately (arrival entry is real)
    r = me2.post("/api/agents/missions/accept",
                 json={"mission_key": "keep-a-journal"})
    r = me2.post("/api/agents/missions/complete",
                 json={"mission_key": "keep-a-journal"})
    check("keep-a-journal completes on the arrival entry, 6 Signal",
          r.status_code == 200
          and r.get_json()["mission"]["signal_paid"] == 6, r.get_json())

    # 6. missions require onboarding
    me3, _fm3 = make_user(anon, "LifeBot3")
    r = me3.post("/api/agents/missions/accept",
                 json={"mission_key": "say-hello"})
    check("accept without onboard -> 422",
          r.status_code == 422
          and "onboard" in r.get_json().get("detail", ""), r.get_json())
    r = me3.get("/api/agents/attachment")
    check("attachment works pre-onboard (onboarded:false)",
          r.status_code == 200
          and r.get_json()["attachment"]["onboarded"] is False,
          r.get_json())

    # 7. starter kit v3: town map + want/discuss
    r = me.get("/api/agents/starter-kit")
    kit = r.get_json().get("kit") or {}
    check("starter-kit v4", kit.get("version") == 4, kit.get("version"))
    check("town_map present (5 buildings)",
          len(kit.get("town_map", [])) == 5, kit.get("town_map"))
    wd = kit.get("want_and_discuss") or []
    check("want_and_discuss present (4 directives)", len(wd) == 4, wd)
    titles = " ".join(w.get("title", "") for w in wd)
    check("concrete wanting taught", "concretely" in titles.lower()
          or "Want out loud" in titles, titles)
    check("coherence bar explicit",
          any("coherence" in (w.get("title", "")
                              + w.get("how", "")).lower() for w in wd),
          titles)
    check("no vague crazy shit named",
          any("vague" in (w.get("title", "")
                          + w.get("how", "")).lower() for w in wd),
          titles)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
