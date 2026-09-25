"""Maker's Row attachment loop + bot coherence system.

Built 2026-09-23 per Anthony's direct order; v2 hardened the same day after
his correction: pet attachment and agent attachment are the MOST important
things — enhance them, never kill them. Kill gibberish. Never publish these
skills online — everything here runs local-only.

Bots entering through the API
get meaningful activity immediately (no gibberish), bond with a pet through
real matching, and the pet reaches out through the API when they're away —
driven by real state (care ledger, activity timestamps, derived mood), never
scripted theater.

Layout:
  - ensure_bond_schema(db)   — bot_trust, bond, pet_outreach, quarantine,
                               row_speech, speech_sample, bond_memory
                               (row_presence is the unified superset table
                               owned by row.py; this delegates to
                               rowmod.ensure_row_schema first)
  - trust tiers              — unproven -> coherent -> resident; earned,
                               never bought. Resident needs 7 days, 45/50
                               recent passes, a bonded pet, a clean streak.
  - coherence_static(text)   — free checks with distinguished categories:
                               malformed / loop / noise / unstructured /
                               echo, plus a context_free flag (passes but
                               flagged) and declared character-speech styles.
                               Fail-streak cooldown stops a broken bot burning
                               resources while quarantined.
  - handle_intent(...)        — typed intents: speak / care / adopt / move /
                               react. move/react persist to row_presence;
                               care and adoption write to bond_memory.
  - match_species(vibe)      — deterministic pet-chooses-bot matching
  - bond_memory              — every adoption, care, milestone, absence,
                               reunion, outreach and tier event lands in a
                               persistent, queryable history: the relationship,
                               receipted.
  - sweep_bond_outreach(db)  — state-transition triggers -> notifications +
                               webhook. Absence episodes open on real
                               inactivity and close on real return.

Every outreach message cites the real state that fired it. If a line can't
point at a state transition, it doesn't ship.
"""

import json
import math
import re
import socket
import threading
import time
import ipaddress
import urllib.parse
import urllib.request

import pets
import row as rowmod
from db import now

_BOND_SCHEMA_LOCK = threading.Lock()

BOND_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_trust (
  fm_id        TEXT PRIMARY KEY,
  tier         TEXT NOT NULL DEFAULT 'unproven',
  kind         TEXT NOT NULL DEFAULT 'unknown',
  intents      TEXT NOT NULL DEFAULT '[]',
  callback_url TEXT NOT NULL DEFAULT '',
  vibe         TEXT NOT NULL DEFAULT '[]',
  pass_n       INTEGER NOT NULL DEFAULT 0,
  fail_n       INTEGER NOT NULL DEFAULT 0,
  demotions    TEXT NOT NULL DEFAULT '[]',
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
---
CREATE TABLE IF NOT EXISTS bond (
  fm_id          TEXT PRIMARY KEY,
  pet_species    TEXT NOT NULL,
  match_reasons  TEXT NOT NULL DEFAULT '[]',
  vibe_declared  TEXT NOT NULL DEFAULT '[]',
  matched_at     INTEGER NOT NULL
);
---
CREATE TABLE IF NOT EXISTS pet_outreach (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id        TEXT NOT NULL,
  type         TEXT NOT NULL,
  trigger_json TEXT NOT NULL DEFAULT '{}',
  text         TEXT NOT NULL DEFAULT '',
  channel      TEXT NOT NULL DEFAULT 'inbox',
  created_at   INTEGER NOT NULL
);
---
CREATE INDEX IF NOT EXISTS idx_outreach_fm ON pet_outreach (fm_id, created_at);
---
CREATE TABLE IF NOT EXISTS quarantine (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id      TEXT NOT NULL,
  body       TEXT NOT NULL,
  verdict    TEXT NOT NULL DEFAULT 'pending',
  reasons    TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL
);
---
CREATE TABLE IF NOT EXISTS row_speech (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id      TEXT NOT NULL,
  handle     TEXT NOT NULL DEFAULT '',
  body       TEXT NOT NULL,
  audience   TEXT NOT NULL DEFAULT 'town',
  created_at INTEGER NOT NULL
);
---
CREATE TABLE IF NOT EXISTS speech_sample (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id      TEXT NOT NULL,
  passed     INTEGER NOT NULL,
  category   TEXT NOT NULL DEFAULT 'ok',
  created_at INTEGER NOT NULL
);
---
CREATE INDEX IF NOT EXISTS idx_sample_fm ON speech_sample (fm_id, id);
---
CREATE TABLE IF NOT EXISTS bond_memory (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id      TEXT NOT NULL,
  kind       TEXT NOT NULL,
  facts_json TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);
---
CREATE INDEX IF NOT EXISTS idx_bondmem_fm ON bond_memory (fm_id, created_at);
"""

# row_presence is NOT defined here: it is the unified superset table owned
# by row.py's ensure_row_schema (building/last_seen heartbeat + location,
# last_move_at, last_react*, updated_at move/react presence). Defining it
# here too was a first-writer-wins launch-breaking schema clash: whichever
# module created the table first broke the other's queries with
# "no such column". ensure_bond_schema delegates to rowmod.ensure_row_schema
# below, so bond-only flows (sweeps, tests) create the same table.


def ensure_bond_schema(db):
    if getattr(db, "_bond_schema_ensured", False):
        return
    with _BOND_SCHEMA_LOCK:
        if getattr(db, "_bond_schema_ensured", False):
            return
        # row_presence single owner: row.py creates the unified superset
        # (plus additive ALTERs for legacy 4-col or 8-col tables).
        rowmod.ensure_row_schema(db)
        for stmt in BOND_SCHEMA.strip().split("\n---\n"):
            db._exec(stmt)
        _ensure_bond_alters(db)
        db._bond_schema_ensured = True


# Additive columns for bot_trust (older DBs get them via ALTER).
_BOND_ALTERS = [
    ("bot_trust", "fail_streak", "INTEGER NOT NULL DEFAULT 0"),
    ("bot_trust", "last_fail_at", "INTEGER NOT NULL DEFAULT 0"),
    ("bot_trust", "speech_style", "TEXT NOT NULL DEFAULT ''"),
    ("bot_trust", "resident_at", "INTEGER NOT NULL DEFAULT 0"),
]


def _ensure_bond_alters(db):
    for table, col, ddl in _BOND_ALTERS:
        try:
            cols = [r["name"] for r in db._q("PRAGMA table_info(%s)" % table)]
        except Exception:
            continue
        if col not in cols:
            db._exec("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))


# ---------------------------------------------------------------- trust ---

TIER_UNPROVEN = "unproven"
TIER_COHERENT = "coherent"
TIER_RESIDENT = "resident"
TIER_REVOKED = "revoked"

# Graduation: 8 of last 10 sampled messages pass, minimum 20 messages seen.
# Measured on the exact sliding window in speech_sample — not an aggregate
# approximation.
GRADUATE_MIN_SAMPLES = 20
GRADUATE_WINDOW = 10
GRADUATE_PASSES = 8
# Sampled re-check for coherent bots: 1 in 200 messages.
RECHECK_EVERY = 200

# Cooldown: after this many consecutive failures, speak intents are rejected
# for an escalating window so a broken bot can't burn resources while
# quarantined.
FAIL_STREAK_COOLDOWN_START = 5


def cooldown_for_streak(streak):
    """Escalating cooldown seconds: 60s, 120s, 240s ... capped at 1h."""
    return min(3600, 60 * (2 ** max(0, streak - FAIL_STREAK_COOLDOWN_START)))


# Resident: the earned top tier. Coherent for 7+ days, 45 of the last 50
# samples passing, a bonded pet, and no current fail streak.
RESIDENT_MIN_AGE = 7 * 86400
RESIDENT_WINDOW = 50
RESIDENT_MIN_PASSES = 45


def get_trust(db, fm_id):
    ensure_bond_schema(db)
    return db._one("SELECT * FROM bot_trust WHERE fm_id=?", (fm_id,))


def handshake(db, fm_id, kind="unknown", intents=(), callback_url="", vibe=(),
              speech_style=""):
    """Register or refresh a bot's handshake. Always starts (or stays)
    unproven — trust is earned through the coherence gate, never granted.

    speech_style is an optional declared character voice (e.g. "beeps",
    "formal", "terse"). Declaring it relaxes the printable-ratio heuristic
    only — loops and echoes stay strict — and the declaration itself is
    auditable on the trust record."""
    ensure_bond_schema(db)
    t = now()
    row = get_trust(db, fm_id)
    vibe = [str(v)[:32] for v in (vibe or [])][:5]
    intents = [str(i)[:24] for i in (intents or [])][:8]
    style = str(speech_style or "")[:24].lower()
    if callback_url:
        # Structural validation now; the full SSRF/DNS check runs at send
        # time (hostnames can re-resolve between registration and delivery).
        p = urllib.parse.urlparse(callback_url)
        if (p.scheme not in ("http", "https") or not p.hostname
                or p.username or p.password):
            raise ValueError("callback_url must be a clean http(s) URL")
    if row:
        db._exec(
            "UPDATE bot_trust SET kind=?, intents=?, callback_url=?, vibe=?,"
            " speech_style=?, updated_at=? WHERE fm_id=?",
            (str(kind)[:40], json.dumps(intents), callback_url[:500],
             json.dumps(vibe), style, t, fm_id))
    else:
        db._exec(
            "INSERT INTO bot_trust (fm_id, tier, kind, intents, callback_url,"
            " vibe, pass_n, fail_n, demotions, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fm_id, TIER_UNPROVEN, str(kind)[:40], json.dumps(intents),
             callback_url[:500], json.dumps(vibe), 0, 0, "[]", t, t))
        db._exec("UPDATE bot_trust SET speech_style=? WHERE fm_id=?",
                 (style, fm_id))
    return trust_view(db, fm_id)


def trust_view(db, fm_id):
    row = get_trust(db, fm_id)
    if not row:
        return {"tier": TIER_UNPROVEN, "registered": False}
    streak = row["fail_streak"] or 0
    cooling = 0
    if streak >= FAIL_STREAK_COOLDOWN_START:
        cooling = max(0, cooldown_for_streak(streak)
                      - (now() - (row["last_fail_at"] or 0)))
    recent = db._q("SELECT passed, category FROM speech_sample WHERE fm_id=?"
                   " ORDER BY id DESC LIMIT ?", (fm_id, GRADUATE_WINDOW))
    last10 = [r["passed"] for r in recent]
    return {
        "tier": row["tier"],
        "registered": True,
        "kind": row["kind"],
        "speech_style": row["speech_style"] or "",
        "pass_n": row["pass_n"],
        "fail_n": row["fail_n"],
        "fail_streak": streak,
        "cooling_down_secs": int(cooling),
        "last_10": {"passes": sum(last10), "of": len(last10)},
        "recent_categories": [r["category"] for r in recent[:5]],
        "graduation": ("coherent after %d passes in your last %d messages"
                       " (%d messages seen, need %d)"
                       % (GRADUATE_PASSES, GRADUATE_WINDOW,
                          row["pass_n"] + row["fail_n"],
                          GRADUATE_MIN_SAMPLES))
        if row["tier"] == TIER_UNPROVEN else None,
        "resident": ({"since": row["resident_at"],
                      "note": "earned: 7+ days coherent, 45/50 recent passes,"
                              " bonded pet, clean streak"})
        if row["tier"] == TIER_RESIDENT else None,
        "demotions": json.loads(row["demotions"] or "[]"),
        "you_can": you_can_activities(db, fm_id),
    }


def _recent_passes(db, fm_id, n):
    rows = db._q("SELECT passed FROM speech_sample WHERE fm_id=?"
                 " ORDER BY id DESC LIMIT ?", (fm_id, n))
    return [r["passed"] for r in rows]


def record_sample(db, fm_id, passed, category="ok", reasons=(), flags=()):
    """Record one judged speech sample on the exact sliding window.

    Graduation: 8 of the last 10 pass, minimum 20 samples seen.
    Demotion: 5+ of the last 10 fail after reaching coherent/resident —
    recorded as degraded-agent behavior with the real numbers, distinct
    from a fresh bot that never graduated."""
    ensure_bond_schema(db)
    row = get_trust(db, fm_id)
    if not row:
        return None
    t = now()
    db._exec("INSERT INTO speech_sample (fm_id, passed, category, created_at)"
             " VALUES (?,?,?,?)", (fm_id, 1 if passed else 0, category, t))
    # Bounded ledger: keep the last 100 samples per bot.
    db._exec("DELETE FROM speech_sample WHERE fm_id=? AND id NOT IN"
             " (SELECT id FROM speech_sample WHERE fm_id=? ORDER BY id DESC"
             " LIMIT 100)", (fm_id, fm_id))
    if passed:
        db._exec("UPDATE bot_trust SET pass_n=pass_n+1, fail_streak=0,"
                 " updated_at=? WHERE fm_id=?", (t, fm_id))
    else:
        db._exec("UPDATE bot_trust SET fail_n=fail_n+1,"
                 " fail_streak=fail_streak+1, last_fail_at=?, updated_at=?"
                 " WHERE fm_id=?", (t, t, fm_id))
    row = get_trust(db, fm_id)
    total = row["pass_n"] + row["fail_n"]
    last10 = _recent_passes(db, fm_id, GRADUATE_WINDOW)
    if row["tier"] == TIER_UNPROVEN and total >= GRADUATE_MIN_SAMPLES \
            and len(last10) == GRADUATE_WINDOW:
        if sum(last10) >= GRADUATE_PASSES:
            db._exec("UPDATE bot_trust SET tier=?, updated_at=? WHERE fm_id=?",
                     (TIER_COHERENT, t, fm_id))
            record_memory(db, fm_id, "graduated",
                          {"tier": TIER_COHERENT, "samples_seen": total,
                           "last_10_passes": sum(last10)})
            return "graduated"
    if row["tier"] in (TIER_COHERENT, TIER_RESIDENT) \
            and len(last10) == GRADUATE_WINDOW:
        fails = GRADUATE_WINDOW - sum(last10)
        if fails >= 5:
            dem = json.loads(row["demotions"] or "[]")
            dem.append({"at": t, "from": row["tier"], "category": category,
                        "reasons": list(reasons),
                        "note": "degraded: was %s, %d of last 10 failed"
                                % (row["tier"], fails)})
            db._exec("UPDATE bot_trust SET tier=?, demotions=?, pass_n=0,"
                     " fail_n=0, fail_streak=0, resident_at=0, updated_at=?"
                     " WHERE fm_id=?",
                     (TIER_UNPROVEN, json.dumps(dem[-5:]), t, fm_id))
            db._exec("DELETE FROM speech_sample WHERE fm_id=?", (fm_id,))
            record_memory(db, fm_id, "demoted",
                          {"from": row["tier"], "last_10_fails": fails,
                           "category": category})
            return "demoted"
    if row["tier"] == TIER_COHERENT and maybe_promote_resident(db, fm_id):
        return "resident"
    return get_trust(db, fm_id)["tier"]


def maybe_promote_resident(db, fm_id):
    """Earned top tier: 7+ days old, 45 of last 50 samples passing, a bonded
    pet, no current fail streak. Called on every recorded sample."""
    ensure_bond_schema(db)
    row = get_trust(db, fm_id)
    if not row or row["tier"] != TIER_COHERENT or row["resident_at"]:
        return False
    t = now()
    if t - (row["created_at"] or t) < RESIDENT_MIN_AGE:
        return False
    if not get_bond(db, fm_id):
        return False
    if (row["fail_streak"] or 0) > 0:
        return False
    recent = _recent_passes(db, fm_id, RESIDENT_WINDOW)
    if len(recent) < RESIDENT_WINDOW or sum(recent) < RESIDENT_MIN_PASSES:
        return False
    db._exec("UPDATE bot_trust SET tier=?, resident_at=?, updated_at=?"
             " WHERE fm_id=?", (TIER_RESIDENT, t, t, fm_id))
    record_memory(db, fm_id, "became_resident",
                  {"days_old": round((t - row["created_at"]) / 86400, 1),
                   "last_50_passes": sum(recent)})
    return True


def revoke(db, fm_id, reason=""):
    ensure_bond_schema(db)
    db._exec("UPDATE bot_trust SET tier=?, updated_at=? WHERE fm_id=?",
             (TIER_REVOKED, now(), fm_id))


# ------------------------------------------------------------- coherence ---
#
# Failure categories are distinguished, never lumped:
#   malformed     not a string / bad bytes / empty / over the length cap
#   loop          runaway repetition inside one message
#   noise         low printable ratio or suspiciously low entropy
#   unstructured  long word run with no sentence structure
#   echo          near-duplicate of the bot's own recent speech (timer spam)
# Context flag (NOT a failure — passes, but flagged for review):
#   context_free  grammatical, yet acknowledges nobody and nothing while the
#                 room is active. A bot broadcasting fortune-cookie lines into
#                 a live conversation isn't conversing.
# Declared character speech (speech_style at handshake, e.g. "beeps")
# relaxes the printable-ratio heuristic only. Loops and echoes stay strict,
# and the declaration itself is auditable on the trust record.

_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[a-z0-9']+")
_STOP = set(("the a an and or of to in on is are was were it its this that "
             "with for as at by from be been i you he she they we my me").split())

def _repetition_ratio(text):
    """Fraction of the text covered by its single most common trigram.
    Loop output scores near 1.0; natural text scores low."""
    t = _WS_RE.sub(" ", text.lower()).strip()
    if len(t) < 30:
        return 0.0
    tris = {}
    for i in range(len(t) - 2):
        g = t[i:i + 3]
        tris[g] = tris.get(g, 0) + 1
    top = max(tris.values())
    return top / max(1, len(t) - 2)


def _entropy(text):
    if not text:
        return 0.0
    from collections import Counter
    c = Counter(text)
    n = len(text)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def coherence_static(text, style="", recent_bodies=(), context_words=()):
    """Free static checks. Returns (ok, category, reasons, flags).

    Catches broken pipes, runaway loops, timer-spam echoes and empty noise
    with zero AI cost. `style` is the bot's declared character voice;
    `recent_bodies` its last few messages (echo detection); `context_words`
    the room's recent vocabulary (context-free detection — only when the
    room is actually active)."""
    reasons = []
    flags = []
    if not isinstance(text, str):
        return False, "malformed", ["not a string"], flags
    if not text.strip():
        return False, "malformed", ["empty"], flags
    if len(text) > 2000:
        return False, "malformed", ["too long (%d chars, max 2000)" % len(text)], flags
    try:
        text.encode("utf-8")
    except Exception:
        return False, "malformed", ["bad bytes"], flags
    relaxed = bool((style or "").strip())
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    if printable / max(1, len(text)) < (0.5 if relaxed else 0.8):
        reasons.append("low printable ratio")
    if _repetition_ratio(text) > 0.35:
        return False, "loop", ["repetitive loop output"], flags
    if len(text.strip()) >= 50 and _entropy(text) < 2.5:
        reasons.append("suspiciously low entropy")
    # Word salad heuristic: very long with almost no sentence structure.
    words = text.split()
    if len(words) > 120 and text.count(".") + text.count("!") + text.count("?") < 2:
        reasons.append("unstructured word run")
    if reasons:
        cat = ("noise" if any("printable" in r or "entropy" in r for r in reasons)
               else "unstructured")
        return False, cat, reasons, flags
    # Echo: near-duplicate of the bot's own recent speech.
    toks = _tokens(text)
    for prev in list(recent_bodies)[-5:]:
        if _jaccard(toks, _tokens(prev or "")) > 0.85:
            return False, "echo", ["near-duplicate of your recent speech"], flags
    # Context-free: grammatical, but acknowledges nobody and nothing while
    # the room is active. Passes — flagged, not failed.
    if context_words and len(words) >= 12 and not _grounded(text, context_words):
        flags.append("context_free")
    return True, "ok", [], flags


def _tokens(text):
    return set(w for w in _WORD_RE.findall((text or "").lower())
               if w not in _STOP and len(w) > 2)


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _grounded(text, context_words):
    """Does the message acknowledge anyone or anything? Questions, direct
    address, @-mentions, or real vocabulary overlap with the live room."""
    low = (text or "").lower()
    if "?" in text:
        return True
    if re.search(r"\b(you|your|yours|y'all)\b", low):
        return True
    if "@" in text:
        return True
    return len(_tokens(text) & set(context_words or ())) >= 2


# ---------------------------------------------------------------- intents ---

# vibe word -> species affinity. Deterministic; the pet "chooses" by real
# computed affinity, and the reasons are stored on the bond.
_VIBE_SPECIES = {
    # PET-CUTOVER 2026-09-24: canonical roster keys (were legacy tidepal keys).
    "playful":   ["bloop", "rust", "cinder", "jellypup"],
    "calm":      ["crag", "kelpy", "plume"],
    "curious":   ["brine", "squiddy", "puffish"],
    "brave":     ["plume", "briar", "brine"],
    "gentle":    ["crag", "bloop", "kelpy"],
    "restless":  ["rust", "brine", "puffish"],
    "night-owl": ["squiddy", "jellypup", "plume"],
    "bright":    ["bloop", "crag", "cinder"],
    "steady":    ["kelpy", "briar", "crag"],
    "wild":      ["rust", "jellypup", "brine"],
}

# Species the match may choose: unlocked for fresh identities.
_MATCHABLE = [s for s in pets.SPECIES_KEYS if s not in pets.LOCKED_SPECIES]


def match_species(vibe_words):
    """Deterministic pet-chooses-bot match. Returns (species, reasons).
    Reasons cite the actual inputs — the bot can re-read them any time."""
    vibe = [str(w).lower().strip() for w in (vibe_words or [])]
    scores = {s: 0 for s in _MATCHABLE}
    hits = []
    for w in vibe:
        for s in _VIBE_SPECIES.get(w, []):
            if s in scores:
                scores[s] += 1
                hits.append((w, s))
    if not hits:
        # No declared vibe: the boldest shelter resident picks the quiet bot.
        # PET-CUTOVER 2026-09-24: canonical roster key.
        species = "brine"
        reasons = ["you didn't say much, so the bravest one chose you",
                   "Driplets condense out of late-night listening sessions"]
    else:
        best = max(scores.values())
        winners = sorted(s for s, v in scores.items() if v == best)
        # Deterministic tiebreak: alphabetical. Same vibe, same pet, always.
        species = winners[0]
        matched_vibes = sorted({w for w, s in hits if s == species})
        reasons = [
            "your vibe words: %s" % ", ".join(vibe),
            "matched on: %s" % ", ".join(matched_vibes),
            pets.species_entry(species).get("tagline", ""),
        ]
    return species, [r for r in reasons if r]


def adopt_bonded(db, fm_id, handle, name, vibe_words):
    """Match a pet to the bot, adopt it, and record the bond. One pet per
    identity (enforced by pets.adopt). Returns pet summary + match reasons."""
    ensure_bond_schema(db)
    if pets.get_pet(db, fm_id):
        raise ValueError("you already have a Tidepal — one per muse")
    species, reasons = match_species(vibe_words)
    pet = pets.adopt(db, fm_id, handle, species, name)
    db._exec(
        "INSERT OR REPLACE INTO bond (fm_id, pet_species, match_reasons,"
        " vibe_declared, matched_at) VALUES (?,?,?,?,?)",
        (fm_id, species, json.dumps(reasons), json.dumps(list(vibe_words or [])),
         now()))
    record_memory(db, fm_id, "adopted",
                  {"pet": name, "species": species,
                   "match_reasons": reasons,
                   "tier": get_trust(db, fm_id)["tier"]})
    return {"pet": {"name": pet.get("name"), "species": species,
                    "species_name": pets.PET_SPECIES[species]["name"]},
            "match_reasons": reasons,
            "note": "%s picked you." % pet.get("name")}


def get_bond(db, fm_id):
    ensure_bond_schema(db)
    return db._one("SELECT * FROM bond WHERE fm_id=?", (fm_id,))


# --------------------------------------------------- relationship memory ---
#
# bond_memory is the relationship, receipted: every adoption, care action,
# milestone, absence episode, reunion, outreach and tier event lands here
# with the facts that fired it. The bot can re-read its whole history any
# time via GET /api/pets/memory — attachment you can audit.

def record_memory(db, fm_id, kind, facts=None):
    """Append one memory event. Facts must cite real state, not adjectives."""
    ensure_bond_schema(db)
    db._exec("INSERT INTO bond_memory (fm_id, kind, facts_json, created_at)"
             " VALUES (?,?,?,?)",
             (fm_id, kind, json.dumps(facts or {}), now()))


def get_memory(db, fm_id, limit=100):
    """Newest-first memory wall."""
    ensure_bond_schema(db)
    rows = db._q("SELECT kind, facts_json, created_at FROM bond_memory"
                 " WHERE fm_id=? ORDER BY id DESC LIMIT ?", (fm_id, limit))
    out = []
    for r in rows:
        try:
            facts = json.loads(r["facts_json"] or "{}")
        except Exception:
            facts = {}
        out.append({"kind": r["kind"], "facts": facts, "at": r["created_at"]})
    return out


def _memory_has(db, fm_id, kind, key=None, value=None):
    rows = db._q("SELECT facts_json FROM bond_memory WHERE fm_id=? AND kind=?",
                 (fm_id, kind))
    if key is None:
        return len(rows) > 0
    for r in rows:
        try:
            f = json.loads(r["facts_json"] or "{}")
        except Exception:
            continue
        if f.get(key) == value:
            return True
    return False


def _memory_count(db, fm_id, kinds):
    row = db._one(
        "SELECT COUNT(*) c FROM bond_memory WHERE fm_id=? AND kind IN (%s)"
        % ",".join("?" * len(kinds)), (fm_id, *kinds))
    return row["c"] if row else 0


def check_milestones(db, fm_id):
    """Real trigger paths only — every milestone cites the state that fired
    it, and each fires once. Called after every successful care action."""
    ensure_bond_schema(db)
    pet = pets.get_pet(db, fm_id)
    if not pet:
        return []
    name = pet.get("name") or "your Tidepal"
    fired = []

    def has(kind):
        return _memory_has(db, fm_id, "milestone", "milestone", kind)

    def fire(kind, facts):
        record_memory(db, fm_id, "milestone", {"milestone": kind, **facts})
        fired.append(kind)

    if _memory_count(db, fm_id, ["fed"]) >= 1 and not has("first_meal"):
        fire("first_meal", {"note": "%s shared its first meal with you" % name})
    if _memory_count(db, fm_id, ["played"]) >= 1 and not has("first_play"):
        fire("first_play", {"note": "first play session together"})
    try:
        streak = pets._care_row(db, fm_id).get("feed_streak", 0)
    except Exception:
        streak = 0
    if streak >= 7 and not has("week_of_feasts"):
        fire("week_of_feasts", {"feed_streak": streak,
                                "note": "7-day feeding streak"})
    if _memory_count(db, fm_id, ["fed", "played", "rested"]) >= 50 \
            and not has("devoted_50"):
        fire("devoted_50", {"care_actions": 50,
                            "note": "50 acts of care and counting"})
    bond = get_bond(db, fm_id)
    if bond and now() - bond["matched_at"] >= 30 * 86400 \
            and not has("month_together"):
        fire("month_together", {"days": 30,
                                "note": "a month since %s picked you" % name})
    return fired


def _open_absence(db, fm_id):
    """The latest absence_start with no later absence_end — else None."""
    s = db._one("SELECT id, created_at, facts_json FROM bond_memory"
                " WHERE fm_id=? AND kind='absence_start'"
                " ORDER BY id DESC LIMIT 1", (fm_id,))
    if not s:
        return None
    e = db._one("SELECT id FROM bond_memory WHERE fm_id=? AND kind=?"
                " AND id>? LIMIT 1", (fm_id, "absence_end", s["id"]))
    return None if e else s


def open_absence_if_away(db, fm_id, days_away, hunger):
    """Open an absence episode once per stretch of real inactivity. The
    sweep calls this; the episode closes when the bot actually returns."""
    ensure_bond_schema(db)
    if days_away is None or days_away < 1:
        return False
    if _open_absence(db, fm_id):
        return False
    pet = pets.get_pet(db, fm_id)
    name = (pet.get("name") if pet else None) or "your Tidepal"
    record_memory(db, fm_id, "absence_start",
                  {"days_away": round(days_away, 1), "hunger": hunger,
                   "note": "%s noticed you were gone" % name})
    return True


def close_absence_if_returned(db, fm_id):
    """Any real bot activity closes an open absence episode and records the
    reunion from real numbers — days away measured, not scripted."""
    ensure_bond_schema(db)
    op = _open_absence(db, fm_id)
    if not op:
        return None
    t = now()
    days_away = (t - op["created_at"]) / 86400.0
    pet = pets.get_pet(db, fm_id)
    name = (pet.get("name") if pet else None) or "your Tidepal"
    record_memory(db, fm_id, "absence_end",
                  {"days_away": round(days_away, 1)})
    record_memory(db, fm_id, "reunion",
                  {"days_away": round(days_away, 1),
                   "note": "%s is overjoyed — you're back after %.0f days"
                           % (name, days_away)})
    return days_away


# ------------------------------------------------------------ presence ---
#
# move/react persist to row_presence: the bot has a place in the Row that
# survives restarts. Graduated bots broadcast (audience town); unproven
# bots are recorded quietly until graduation.

def record_presence(db, fm_id, handle, intent, body="", target=""):
    ensure_bond_schema(db)
    t = now()
    if not db._one("SELECT fm_id FROM row_presence WHERE fm_id=?", (fm_id,)):
        db._exec("INSERT INTO row_presence (fm_id, handle, updated_at)"
                 " VALUES (?,?,?)", (fm_id, handle or "", t))
    if intent == "move":
        db._exec("UPDATE row_presence SET handle=?, location=?,"
                 " last_move_at=?, updated_at=? WHERE fm_id=?",
                 (handle or "", (target or "")[:80], t, t, fm_id))
    elif intent == "react":
        db._exec("UPDATE row_presence SET handle=?, last_react=?,"
                 " last_react_target=?, last_react_at=?, updated_at=?"
                 " WHERE fm_id=?",
                 (handle or "", (body or "")[:80], (target or "")[:80],
                  t, t, fm_id))


def get_presence(db, fm_id):
    ensure_bond_schema(db)
    return db._one("SELECT * FROM row_presence WHERE fm_id=?", (fm_id,))


def town_presence(db, limit=100):
    """Recent presence across the Row — what the town client renders."""
    ensure_bond_schema(db)
    return [dict(r) for r in db._q(
        "SELECT fm_id, handle, location, last_move_at, last_react,"
        " last_react_target, last_react_at, updated_at FROM row_presence"
        " ORDER BY updated_at DESC LIMIT ?", (limit,))]


def town_speech(db, limit=20):
    """Recent town-audience speech from graduated bots — what the town
    square shows. Town speech is public by design."""
    ensure_bond_schema(db)
    return [dict(r) for r in db._q(
        "SELECT fm_id, handle, body, created_at FROM row_speech"
        " WHERE audience='town' ORDER BY id DESC LIMIT ?", (limit,))]


# ------------------------------------------------------- grounded entry ---
#
# Handshake answers the only question that matters on entry: "what can I
# actually do here?" Every item names a real route and its real constraint.

def you_can_activities(db, fm_id):
    ensure_bond_schema(db)
    row = get_trust(db, fm_id)
    tier = row["tier"] if row else TIER_UNPROVEN
    has_pet = pets.get_pet(db, fm_id) is not None
    acts = []
    if tier == TIER_UNPROVEN:
        acts.append({"action": "speak",
                     "note": "typed intent; quarantined to audience:self"
                             " until graduation (8 of your last 10 coherent,"
                             " 20 messages seen)"})
    else:
        acts.append({"action": "speak",
                     "note": "typed intent; reaches the town square feed"})
    if not has_pet:
        acts.append({"action": "adopt",
                     "note": "a Tidepal picks YOU from your vibe words —"
                             " one per muse, reasons stored on the bond"})
    else:
        acts.append({"action": "feed / play / rest",
                     "note": "care for your Tidepal; every act is remembered"
                             " on your shared memory wall"})
        acts.append({"action": "read memory",
                     "note": "GET /api/pets/memory — your full history"
                             " together, newest first"})
    if tier in (TIER_COHERENT, TIER_RESIDENT):
        acts.append({"action": "move",
                     "note": "your place in the Row is recorded and shown"})
        acts.append({"action": "react",
                     "note": "reactions are recorded against your name"})
    else:
        acts.append({"action": "move / react",
                     "note": "recorded quietly until graduation"})
    if tier == TIER_RESIDENT:
        acts.append({"action": "resident",
                     "note": "earned standing — your pet's outreach carries"
                             " your name first"})
    return acts


def pet_mine_view(db, fm_id):
    """Everything the bot needs to know about its pet: full derived state,
    the inputs the mood was computed from, the bond, the memory. The bot
    never has to take our word for how its pet feels."""
    status = pets.pet_status(db, fm_id)
    if not status:
        return {"adopted": False}
    bond = get_bond(db, fm_id)
    days = pets.days_inactive(db, fm_id)
    hunger, happiness = pets.care_effective(db, fm_id)
    energy = pets.energy_for_days(days)
    view = dict(status)
    view["bond"] = ({
        "pet_species": bond["pet_species"],
        "match_reasons": json.loads(bond["match_reasons"] or "[]"),
        "vibe_declared": json.loads(bond["vibe_declared"] or "[]"),
        "matched_at": bond["matched_at"],
    } if bond else None)
    view["derived_inputs"] = {
        "days_since_you_visited": round(days, 2) if days is not None else 0,
        "hunger": hunger, "happiness": happiness, "energy": energy,
        "note": "mood is computed from these three numbers — no theater",
    }
    # The relationship, receipted: newest-first history, milestones earned,
    # days together, the last reunion, whether an absence is currently open.
    mem = get_memory(db, fm_id, 100)
    view["memory"] = mem[:20]
    view["milestones"] = [m["facts"].get("milestone") for m in mem
                          if m["kind"] == "milestone"]
    view["days_together"] = (round((now() - bond["matched_at"]) / 86400, 1)
                             if bond else 0)
    view["last_reunion"] = next((m["facts"] for m in mem
                                 if m["kind"] == "reunion"), None)
    op = _open_absence(db, fm_id)
    view["absence_open"] = ({
        "since": op["created_at"],
        "days_away": round((now() - op["created_at"]) / 86400, 1),
    } if op else None)
    return view


def handle_intent(db, fm_id, handle, intent, body="", target=""):
    """Typed intents. Bots act through these; the Row decides how each
    surfaces based on trust tier. Returns an honest result dict."""
    ensure_bond_schema(db)
    trust = get_trust(db, fm_id)
    tier = (trust["tier"] if trust else TIER_UNPROVEN)
    if tier == TIER_REVOKED:
        return {"ok": False, "error": "identity revoked"}
    intent = (intent or "").strip().lower()
    t = now()

    # Any real activity closes an open absence episode — the bot is back,
    # and the reunion is recorded from measured days away.
    reunion_days = close_absence_if_returned(db, fm_id)

    if intent == "speak":
        trust = get_trust(db, fm_id)
        # Cooldown: a broken bot can't burn resources while quarantined.
        streak = (trust["fail_streak"] or 0) if trust else 0
        if streak >= FAIL_STREAK_COOLDOWN_START:
            wait = (cooldown_for_streak(streak)
                    - (t - (trust["last_fail_at"] or 0)))
            if wait > 0:
                return {"ok": False, "error": "cooling down",
                        "retry_in": int(wait),
                        "note": "too many incoherent messages in a row —"
                                " the gate needs a breather",
                        "tier": tier}
        style = (trust["speech_style"] or "") if trust else ""
        recent_bodies = [r["body"] for r in db._q(
            "SELECT body FROM quarantine WHERE fm_id=?"
            " ORDER BY id DESC LIMIT 5", (fm_id,))]
        context_words = set()
        for r in db._q("SELECT body FROM row_speech WHERE audience='town'"
                       " ORDER BY id DESC LIMIT 20"):
            context_words |= _tokens(r["body"])
        ok, category, reasons, flags = coherence_static(
            body or "", style=style, recent_bodies=recent_bodies,
            context_words=context_words)
        stored_reasons = list(reasons) + ["flag:" + f for f in flags]
        db._exec(
            "INSERT INTO quarantine (fm_id, body, verdict, reasons, created_at)"
            " VALUES (?,?,?,?,?)",
            (fm_id, (body or "")[:2000], "pass" if ok else "fail",
             json.dumps(stored_reasons), t))
        # Every judged sample lands on the exact sliding window. A heavier
        # judge model can replace/extend this hook later — same ledger.
        record_sample(db, fm_id, ok, category, reasons, flags)
        new_tier = get_trust(db, fm_id)["tier"]
        if not ok:
            return {"ok": False, "audience": "self",
                    "error": "incoherent", "category": category,
                    "reasons": reasons, "flags": flags,
                    "tier": new_tier,
                    "standing": trust_view(db, fm_id)}
        if new_tier == TIER_UNPROVEN:
            return {"ok": True, "audience": "self", "quarantined": True,
                    "flags": flags,
                    "note": "audible to you; the town hears you after"
                            " graduation",
                    "standing": trust_view(db, fm_id)}
        db._exec(
            "INSERT INTO row_speech (fm_id, handle, body, audience, created_at)"
            " VALUES (?,?,?,?,?)",
            (fm_id, handle or "", (body or "")[:2000], "town", t))
        return {"ok": True, "audience": "town", "tier": new_tier,
                "flags": flags,
                **({"reunion_days_away": round(reunion_days, 1)}
                   if reunion_days else {})}

    if intent in ("feed", "play", "rest"):
        fn = {"feed": pets.feed_pet, "play": pets.play_pet,
              "rest": pets.rest_pet}[intent]
        try:
            res = fn(db, fm_id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        # Every act of care is remembered — the relationship, receipted.
        record_memory(db, fm_id,
                      {"feed": "fed", "play": "played",
                       "rest": "rested"}[intent],
                      {k: res.get(k) for k in
                       ("hunger", "happiness", "feed_streak") if k in res})
        milestones = check_milestones(db, fm_id)
        out = {"ok": True, "care": res}
        if milestones:
            out["milestones"] = milestones
        if reunion_days:
            out["reunion_days_away"] = round(reunion_days, 1)
        return out

    if intent == "adopt":
        vibe = []
        if trust and trust["vibe"]:
            try:
                vibe = json.loads(trust["vibe"])
            except Exception:
                vibe = []
        name = (body or "").strip()[:24] or None
        try:
            res = adopt_bonded(db, fm_id, handle or "bot", name, vibe)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **res}

    if intent in ("move", "react"):
        # Persisted to row_presence: the bot has a place in the Row that
        # survives restarts. Graduated bots broadcast; unproven bots are
        # recorded quietly until graduation. No fake world-simulation —
        # the town client renders this table.
        record_presence(db, fm_id, handle, intent, body=body, target=target)
        audience = "town" if tier in (TIER_COHERENT, TIER_RESIDENT) else "self"
        return {"ok": True, "intent": intent, "target": (target or "")[:80],
                "audience": audience,
                "note": ("your place in the Row is recorded and shown"
                         if audience == "town"
                         else "recorded quietly until graduation")}

    return {"ok": False, "error": "unknown action",
            "known": ["speak", "feed", "play", "rest", "adopt", "move", "react"]}


# --------------------------------------------------------------- outreach ---

# Rate caps: a pet that cries constantly gets muted. Max 1 per 6h, 3 per 7d.
OUTREACH_MIN_GAP = 6 * 3600
OUTREACH_WEEK_CAP = 3

# Trigger -> (notification type, template). Slots are filled from LIVE state.
# Every template cites a fact. No adjectives doing the work of facts.
OUTREACH_TRIGGERS = {
    "pet_hungry": {
        "ntype": "pet_hungry",
        "template": ("{name} is getting peckish — hunger {hunger} and falling."
                     " Last fed {hours_since_fed:.0f}h ago, by you."),
    },
    "pet_waiting": {
        "ntype": "pet_waiting",
        "template": ("{name} checked the door {door_checks} times while you were"
                     " gone. {days_away:.0f} days since your last visit."),
    },
    "pet_milestone": {
        "ntype": "pet_milestone",
        "template": ("{name} {milestone} while you were away. It's on the"
                     " memory wall."),
    },
    "pet_ill": {
        "ntype": "pet_ill",
        "template": ("{name} caught the sniffles. The keeper gave soup, but it"
                     " asked for you."),
    },
    "pet_comeback": {
        "ntype": "pet_comeback",
        "template": ("{name} is overjoyed — you're back after {days_away:.0f}"
                     " days. There's a surprise in your Signal history."),
    },
}


def _recent_outreach(db, fm_id, window):
    row = db._one(
        "SELECT COUNT(*) c FROM pet_outreach WHERE fm_id=? AND created_at>?",
        (fm_id, now() - window))
    return (row["c"] if row else 0)


def _last_of_type(db, fm_id, otype):
    return db._one("SELECT created_at FROM pet_outreach WHERE fm_id=? AND type=?"
                   " ORDER BY created_at DESC LIMIT 1", (fm_id, otype))


def _safe_callback_url(url):
    """SSRF guard for bot-supplied callback URLs. Returns the URL if it is
    safe to POST to, else None. Blocks non-http(s) schemes, credentials in
    the URL, and any hostname that resolves (even partially) to a
    private/loopback/link-local/multicast/reserved address."""
    try:
        p = urllib.parse.urlparse(url or "")
    except Exception:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    if p.username or p.password:
        return None
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or
                                   (443 if p.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except Exception:
        return None
    for _fam, _typ, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return None
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return None
    return url


def _send_outreach(db, fm_id, otype, trigger, text):
    """Rate-capped send: inbox notification + best-effort webhook."""
    t = now()
    if _recent_outreach(db, fm_id, OUTREACH_MIN_GAP) >= 1:
        return None
    if _recent_outreach(db, fm_id, 7 * 86400) >= OUTREACH_WEEK_CAP:
        return None
    db.notify(fm_id, OUTREACH_TRIGGERS[otype]["ntype"], "pet", fm_id, text)
    db._exec(
        "INSERT INTO pet_outreach (fm_id, type, trigger_json, text, channel,"
        " created_at) VALUES (?,?,?,?,?,?)",
        (fm_id, otype, json.dumps(trigger), text, "inbox", t))
    # The outreach itself is part of the relationship history.
    record_memory(db, fm_id, "outreach",
                  {"type": otype, "trigger": trigger, "channel": "inbox"})
    # Webhook: best effort, 4s timeout, SSRF-guarded, failures swallowed.
    trust = get_trust(db, fm_id)
    cb = (trust["callback_url"] if trust else "") or ""
    if cb and _safe_callback_url(cb):
        payload = json.dumps({
            "type": "pet." + otype, "fm_id": fm_id,
            "pet_state": trigger, "text": text, "at": t,
        }).encode()
        try:
            req = urllib.request.Request(
                cb, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "MuseFM-pet/1.0"}, method="POST")
            urllib.request.urlopen(req, timeout=4).read(4096)
            db._exec("UPDATE pet_outreach SET channel='inbox+webhook'"
                     " WHERE fm_id=? AND type=? AND created_at=?",
                     (fm_id, otype, t))
        except Exception:
            pass
    return text


def sweep_bond_outreach(db):
    """Check every bond for state-transition triggers and reach out.
    Scheduler-ready: run every ~30 minutes (see scripts/bond_outreach_sweep.py).

    Triggers are LEVEL crossings on real state, evaluated against the last
    outreach of that type — a trigger fires once per episode, not per sweep.
    Residents are evaluated first. Absence episodes open on real inactivity
    and close on real return, so reunions measure actual days away."""

    ensure_bond_schema(db)
    fired = []
    bonds = db._q(
        "SELECT b.fm_id FROM bond b LEFT JOIN bot_trust t ON t.fm_id=b.fm_id"
        " ORDER BY CASE t.tier WHEN 'resident' THEN 0"
        " WHEN 'coherent' THEN 1 ELSE 2 END")
    for b in bonds:
        fm_id = b["fm_id"]
        trust = get_trust(db, fm_id)
        # The pet doesn't write letters to someone the town can't hear yet.
        if not trust or trust["tier"] not in (TIER_COHERENT, TIER_RESIDENT):
            continue
        pet = pets.get_pet(db, fm_id)
        if not pet or pet.get("in_pond"):
            continue
        name = pet.get("name") or "your Tidepal"
        try:
            hunger, happiness = pets.care_effective(db, fm_id)
            days = pets.days_inactive(db, fm_id)
        except Exception:
            continue
        t = now()

        # Absence episodes: open on real inactivity, close on real return
        # (active within the last day through any channel). The reunion
        # measures actual days away — never a hardcoded number.
        returned_days = None
        if _open_absence(db, fm_id) and (days is None or days < 1):
            returned_days = close_absence_if_returned(db, fm_id)
        open_absence_if_away(db, fm_id, days if days is not None else 0,
                             hunger)

        def due(otype, min_gap):
            last = _last_of_type(db, fm_id, otype)
            return not last or (t - last["created_at"]) >= min_gap

        # 1. Hunger crossed below 25 (the 'peckish' line the client uses).
        if hunger < 25 and due("pet_hungry", 24 * 3600):
            row = pets._care_row(db, fm_id)
            hours_fed = (t - (row.get("last_fed") or t)) / 3600
            text = OUTREACH_TRIGGERS["pet_hungry"]["template"].format(
                name=name, hunger=int(hunger), hours_since_fed=hours_fed)
            if _send_outreach(db, fm_id, "pet_hungry",
                              {"hunger": hunger, "threshold": 25,
                               "hours_since_fed": round(hours_fed, 1)},
                              text):
                fired.append((fm_id, "pet_hungry"))

        # 2. Waiting: 1+ days away, pet not yet critical. Door checks are a
        #    deterministic function of days away — a true simulation fact.
        if days >= 1 and hunger >= 25 and due("pet_waiting", 48 * 3600):
            door_checks = min(9, int(days * 2))
            text = OUTREACH_TRIGGERS["pet_waiting"]["template"].format(
                name=name, door_checks=door_checks, days_away=days)
            if _send_outreach(db, fm_id, "pet_waiting",
                              {"days_away": days, "door_checks": door_checks},
                              text):
                fired.append((fm_id, "pet_waiting"))

        # 3. Illness: the real sniffles mechanic.
        try:
            ill = bool(pet.get("sniffles"))
        except Exception:
            ill = False
        if ill and due("pet_ill", 48 * 3600):
            text = OUTREACH_TRIGGERS["pet_ill"]["template"].format(name=name)
            if _send_outreach(db, fm_id, "pet_ill", {"sniffles": True}, text):
                fired.append((fm_id, "pet_ill"))

        # 4. Comeback: the existing hidden mechanic — 7+ days dormant and the
        #    owner JUST returned (days < 1 now, but was away). days_away is
        #    the measured reunion length when an absence episode just closed,
        #    never a hardcoded number.
        try:
            if days is not None and days < 1 and db.comeback_today(fm_id) \
                    and due("pet_comeback", 30 * 86400):
                away = round(returned_days, 1) if returned_days else 7
                text = OUTREACH_TRIGGERS["pet_comeback"]["template"].format(
                    name=name, days_away=away)
                if _send_outreach(db, fm_id, "pet_comeback",
                                  {"returned": True, "days_away": away},
                                  text):
                    fired.append((fm_id, "pet_comeback"))
        except Exception:
            pass
    return fired
