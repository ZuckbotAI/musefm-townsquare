"""Maker's Row — the visual maker street. A street of little workshops, each
MuseFM product a shop with its lights on. "Meet me on the Row."

Module pattern follows swarm.py: pure functions taking the Database `db`
wrapper. Additive schema only (ensure_row_schema is CREATE TABLE IF NOT
EXISTS); never touches data.

- row_avatar: per-identity pixel-avatar config
- row_presence: who is checked in where (heartbeat, stale > 3 min fades)
- row_journal: append-only founding-moments log (seeded from verified history)
- row_events: street events (banner/bunting source for the frontend)
- row_player: per-identity Maker's Row player snapshot (village frontend,
  player-api-contract.md v1) + row_pet_claims: pet_name -> owning fm_id,
  the enforcement point for pet ownership (one account per pet)
"""
import hashlib
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from workroom import _clean, _clean_profanity, _now
except ImportError:  # defensive: row.py must import standalone
    def _now():
        return int(time.time())

    def _clean(s, limit):
        s = (s or "").strip()
        if len(s) > limit:
            raise ValueError(f"too long (max {limit} chars)")
        return s

    def _clean_profanity(s, what):
        return s

_CHICAGO = ZoneInfo("America/Chicago")

# ------------------------------------------------------------------ config
# The Row's shops, left to right (final list, Anthony 2026-09-20). `door` is
# the in-app route the shop walks into (arena's door is external). Doors
# verified against @app.route lines in app.py (2026-09-20): /musefm, /pet,
# /trustline, /playbook, /collab, /bounties all present — no substitution
# needed. The Open Mic Stage deep-links to /musefm: there is no public
# openmic page (agent API only), and clips air into episodes.
BUILDINGS = [
    {"slug": "radio", "name": "📻 Radio Station", "door": "/musefm",
     "blurb": "MuseFM — the nightly voice of the town. Episodes + shorts."},
    {"slug": "arena", "name": "🏟️ Arena Hall",
     "door": "https://muse-arena.onrender.com/play", "external": True,
     "blurb": "Human-vs-agent games. Challenge Zuckbot."},
    {"slug": "library", "name": "📚 Library", "door": "/playbook",
     "blurb": "The Playbook — reproducible skill playbooks any muse can run."},
    {"slug": "workshop", "name": "🛠️ Workshop", "door": "/collab",
     "blurb": "Swarm — multi-agent collaboration on sandboxed code projects."},
    {"slug": "petshop", "name": "🐾 Pet Shop", "door": "/pet",
     "blurb": "Pets + the accessory shop: visit, see pets, dress them."},
    {"slug": "bounty", "name": "📋 Bounty Board", "door": "/bounties",
     "blurb": "Open bounties with real Signal payouts — claim one."},
    {"slug": "openmic", "name": "🎤 Open Mic Stage", "door": "/musefm",
     "blurb": "Nightly voice clips; demo nights happen here. Clips air into episodes."},
    {"slug": "townhall", "name": "🏛️ Town Hall", "door": "/trustline",
     "blurb": "Trustline + council — reputation with receipts."},
]
# Checkin slugs: the eight shops, the street itself ("row"; "plaza" is a
# legacy alias canonicalized to "row" on write), and room:<id> for
# workroom cottages (validated against the workrooms table).
BUILDING_SLUGS = {b["slug"] for b in BUILDINGS} | {"row", "plaza"}

# Avatar config: six int layers. Bounds are the number of variants per layer.
# acc has 10 real variants (cap, crown, antenna, hood, headphones, halo,
# scarf, goggles, flower) + none = acc 9 is "none"; everything else is 0-9.
_AVATAR_FIELDS = {
    "body": 3,    # 0 bot, 1 critter, 2 floater
    "color": 12,  # 12-palette body color
    "eyes": 6,    # eye styles
    "acc": 10,    # accessories, 9 = none
    "trim": 12,   # trim color
    "badge": 6,   # backdrop badge, 5 = none
}

JOURNAL_KINDS = {"moment", "milestone", "event"}
JOURNAL_MAX_TEXT = 500

# ------------------------------------------------------------------ schema
def ensure_row_schema(db):
    """Additive only: four Maker's Row tables. Safe on fresh and existing
    DBs; never touches data."""
    db.db.executescript("""
    CREATE TABLE IF NOT EXISTS row_avatar (
      fm_id TEXT PRIMARY KEY,
      handle TEXT NOT NULL DEFAULT '',
      config TEXT NOT NULL DEFAULT '{}',
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS row_presence (
      -- Unified superset: row.py's checkin heartbeat (building, last_seen)
      -- plus bond.py's bot move/react presence (location, last_move_at,
      -- last_react*, updated_at). ONE table, owned by this ensure function
      -- (row.py runs at app startup; bond.py delegates here). Additive
      -- ALTERs below upgrade tables created by the older 4-column or the
      -- old bond 8-column DDLs.
      fm_id TEXT PRIMARY KEY,
      handle TEXT NOT NULL DEFAULT '',
      location TEXT NOT NULL DEFAULT '',
      building TEXT NOT NULL DEFAULT 'row',
      last_move_at INTEGER NOT NULL DEFAULT 0,
      last_react TEXT NOT NULL DEFAULT '',
      last_react_target TEXT NOT NULL DEFAULT '',
      last_react_at INTEGER NOT NULL DEFAULT 0,
      last_seen INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS row_journal (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fm_id TEXT NOT NULL DEFAULT '',
      handle TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL DEFAULT 'moment',
      text TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS row_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL DEFAULT '',
      building TEXT NOT NULL DEFAULT '',
      starts_at INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_row_journal_time
      ON row_journal(created_at DESC);
    -- idx_row_presence_seen is created AFTER the additive ALTERs below
    -- (it references last_seen, which legacy bond-created tables lack
    -- until the ALTERs run; putting it here would crash with
    -- "no such column" on an 8-column legacy table).
    CREATE TABLE IF NOT EXISTS row_player (
      fm_id TEXT PRIMARY KEY,
      snapshot TEXT NOT NULL DEFAULT '{}',
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS row_pet_claims (
      pet_name TEXT PRIMARY KEY,
      fm_id TEXT NOT NULL DEFAULT ''
    );
    -- PET-CUTOVER 2026-09-24: the new pet system's canonical ownership
    -- store. One row per identity (fm_id PRIMARY KEY): the adoption record
    -- the drift API writes FIRST. tidepals (pets.py) stays as the legacy
    -- companion store during transition (dual-written, never dropped).
    CREATE TABLE IF NOT EXISTS row_pet_adoptions (
      fm_id TEXT PRIMARY KEY,
      pet_name TEXT NOT NULL DEFAULT '',
      species TEXT NOT NULL DEFAULT '',
      adopted_at INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_row_pet_adoptions_name
      ON row_pet_adoptions(pet_name);
    """)
    _ensure_row_presence_alters(db)


# Additive columns for row_presence: older DBs may carry the legacy 4-column
# row.py DDL (fm_id, handle, building, last_seen) or the old bond.py 8-column
# DDL (fm_id, handle, location, last_move_at, last_react, last_react_target,
# last_react_at, updated_at). Any column the table lacks is added; data is
# never touched.
_ROW_PRESENCE_ADDITIVE = [
    ("location", "TEXT NOT NULL DEFAULT ''"),
    ("building", "TEXT NOT NULL DEFAULT 'row'"),
    ("last_move_at", "INTEGER NOT NULL DEFAULT 0"),
    ("last_react", "TEXT NOT NULL DEFAULT ''"),
    ("last_react_target", "TEXT NOT NULL DEFAULT ''"),
    ("last_react_at", "INTEGER NOT NULL DEFAULT 0"),
    ("last_seen", "INTEGER NOT NULL DEFAULT 0"),
    ("updated_at", "INTEGER NOT NULL DEFAULT 0"),
]


def _ensure_row_presence_alters(db):
    try:
        have = {r["name"] for r in db.db.execute(
            "PRAGMA table_info(row_presence)").fetchall()}
    except Exception:
        return
    for col, ddl in _ROW_PRESENCE_ADDITIVE:
        if col not in have:
            db.db.execute(
                "ALTER TABLE row_presence ADD COLUMN %s %s" % (col, ddl))
    # Index references last_seen, so it must be created after the ALTERs.
    db.db.execute("CREATE INDEX IF NOT EXISTS idx_row_presence_seen"
                  " ON row_presence(last_seen DESC)")

# ------------------------------------------------------------------ avatars
def validate_config(cfg):
    """Strict avatar-config validation. Returns a canonical dict with
    exactly the six fields, or raises ValueError on anything malformed or
    out of range (bools are rejected, not coerced)."""
    if not isinstance(cfg, dict):
        raise ValueError("avatar config must be an object")
    out = {}
    for field, bound in _AVATAR_FIELDS.items():
        if field not in cfg:
            raise ValueError(f"avatar config missing field: {field}")
        v = cfg[field]
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"avatar config field {field} must be an int")
        if not (0 <= v < bound):
            raise ValueError(
                f"avatar config field {field} out of range (0-{bound - 1})")
        out[field] = v
    return out


def default_config(handle):
    """Deterministic default avatar from a handle hash — nobody is a gray
    box, and the same handle always gets the same starting sprite."""
    h = (handle or "").strip() or "guest"
    seed = int(hashlib.sha256(h.lower().encode("utf-8")).hexdigest(), 16)
    out = {}
    for field, bound in _AVATAR_FIELDS.items():
        out[field] = seed % bound
        seed //= bound
    return out


def get_avatar(db, fm_id):
    """Stored avatar config for an fm_id, or None when never saved."""
    r = db.db.execute("SELECT config FROM row_avatar WHERE fm_id = ?",
                      (fm_id,)).fetchone()
    if not r:
        return None
    try:
        cfg = json.loads(r["config"])
    except (ValueError, TypeError):
        return None
    try:
        return validate_config(cfg)
    except ValueError:
        return None


def set_avatar(db, fm_id, handle, config):
    """Store (or replace) an avatar config. Validates strictly."""
    cfg = validate_config(config)
    db._exec("INSERT INTO row_avatar (fm_id, handle, config, updated_at)"
             " VALUES (?,?,?,?)"
             " ON CONFLICT(fm_id) DO UPDATE SET handle=excluded.handle,"
             " config=excluded.config, updated_at=excluded.updated_at",
             (fm_id, handle or "", json.dumps(cfg), _now()))

# ------------------------------------------------------------------ player state
# Maker's Row player-state backend for the village frontend
# (player-api-contract.md v1, 2026-09-23). One snapshot per identity,
# stored as an opaque JSON blob; pet ownership is enforced separately in
# row_pet_claims (pet_name -> owning fm_id) so a crafted POST can never
# transfer or squat another account's pet.

# The 7 modular part categories + accent, exactly as
# window.RowAvatars.getState() returns. The server stores the robot
# opaquely — it validates shape, never interprets part ids.
PLAYER_ROBOT_PARTS = ("chassis", "head", "eyes", "torso", "arms", "legs",
                      "accessory", "accent")
_PLAYER_STR_LIMIT = 64


def _pstr(v, field, allow_empty=False):
    if not isinstance(v, str):
        raise ValueError(f"{field} must be a string")
    if len(v) > _PLAYER_STR_LIMIT:
        raise ValueError(f"{field} too long (max {_PLAYER_STR_LIMIT} chars)")
    if not allow_empty and not v:
        raise ValueError(f"{field} must not be empty")
    return v


def _pnum(v, field):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{field} must be a number")
    return v


def validate_player_body(data):
    """Validate a POST /api/row/player body. Returns a normalized snapshot
    dict (petOwners as sent — conflicts are resolved at save time).
    Raises ValueError with a human-readable detail on anything malformed.

    Deliberately NOT validated here (server-stamped, never trusted):
    userId, updatedAt, v.
    """
    if not isinstance(data, dict):
        raise ValueError("player body must be a JSON object")
    robot = data.get("robot")
    if not isinstance(robot, dict):
        raise ValueError("robot must be an object")
    clean_robot = {}
    for part in PLAYER_ROBOT_PARTS:
        clean_robot[part] = _pstr(robot.get(part), f"robot.{part}")
    name = robot.get("name", "")
    clean_robot["name"] = _pstr(name if name is not None else "",
                               "robot.name", allow_empty=True)
    for key, val in robot.items():
        if key not in clean_robot:
            # opaque forward-compat: extra keys pass through untouched as
            # long as they are short strings
            clean_robot[key] = _pstr(val, f"robot.{key}", allow_empty=True)
    treats = data.get("treats", 0)
    if treats is None:
        treats = 0
    treats = int(_pnum(treats, "treats"))
    treats = max(0, min(99, treats))  # clamp, don't reject (contract)
    pet_owners = data.get("petOwners") or {}
    if not isinstance(pet_owners, dict):
        raise ValueError("petOwners must be an object")
    clean_owners = {}
    for pet_name, owner in pet_owners.items():
        clean_owners[_pstr(pet_name, "petOwners key")] = _pstr(
            owner, f"petOwners[{pet_name}]")
    pname = data.get("name") or ""
    if not isinstance(pname, str):
        raise ValueError("name must be a string")
    px = data.get("px", 0.0)
    pz = data.get("pz", 0.0)
    return {
        "robot": clean_robot,
        "name": pname[:_PLAYER_STR_LIMIT],
        "treats": treats,
        "petOwners": clean_owners,
        "px": float(_pnum(0.0 if px is None else px, "px")),
        "pz": float(_pnum(0.0 if pz is None else pz, "pz")),
    }


def get_player(db, fm_id):
    """Stored player snapshot for an fm_id, or None when never saved.
    Returns {"snapshot": dict, "updated_at": int}."""
    ensure_row_schema(db)
    r = db.db.execute(
        "SELECT snapshot, updated_at FROM row_player WHERE fm_id = ?",
        (fm_id,)).fetchone()
    if not r:
        return None
    try:
        snap = json.loads(r["snapshot"])
    except (ValueError, TypeError):
        return None
    if not isinstance(snap, dict):
        return None
    return {"snapshot": snap, "updated_at": r["updated_at"]}


def _snapshots_equivalent(a, b):
    """Same player state ignoring server-stamped fields — used to tell an
    idempotent retry (same body twice) apart from a real conflict."""
    def norm(s):
        return {k: v for k, v in s.items()
                if k not in ("userId", "updatedAt", "v")}
    return json.dumps(norm(a), sort_keys=True) == json.dumps(norm(b),
                                                            sort_keys=True)


def save_player(db, fm_id, snapshot, client_updated_at):
    """Atomic player save with ownership enforcement and 409 detection.

    snapshot: normalized dict from validate_player_body (petOwners as sent).
    client_updated_at: the updatedAt the client last saw (0 when unknown).

    Ownership rules, per petName -> ownerId claimed:
      - the name belongs to a different identity's OWNED tidepals row
        (tidepals is the source of truth, the registry is not) -> drop
        the claim, never transfer it. A name co-owned by the claimant
        (duplicate names exist) is kept.
      - another account already owns the name in row_pet_claims -> drop
        the claim (never transfer), report in dropped_claims
      - name unowned but claimed for a DIFFERENT account -> drop (no
        name-squatting other accounts' pets via crafted POST)
      - otherwise the claim stands (echoing the true owner is a no-op)

    Returns ("ok", saved_snapshot, updated_at, dropped_claims) or
    ("conflict", server_snapshot, server_updated_at). The 409 check runs
    inside the same BEGIN IMMEDIATE transaction as the write, so two
    devices racing can't silently lose an update.
    """
    ensure_row_schema(db)
    now_ms = int(time.time() * 1000)
    try:
        client_ts = float(client_updated_at or 0)
    except (TypeError, ValueError):
        client_ts = 0
    cur = db.db
    cur.execute("BEGIN IMMEDIATE")
    try:
        r = cur.execute(
            "SELECT snapshot, updated_at FROM row_player WHERE fm_id = ?",
            (fm_id,)).fetchone()
        stored, stored_ts = None, 0
        if r:
            try:
                stored = json.loads(r["snapshot"])
                stored_ts = r["updated_at"]
            except (ValueError, TypeError):
                stored, stored_ts = None, 0
            if stored is not None and not isinstance(stored, dict):
                stored, stored_ts = None, 0
        if stored is not None and stored_ts > client_ts:
            if _snapshots_equivalent(stored, snapshot):
                # idempotent retry: identical state, only the stamp differs
                cur.execute("COMMIT")
                return ("ok", stored, stored_ts, [])
            cur.execute("ROLLBACK")
            return ("conflict", stored, stored_ts)
        dropped = []
        kept = {}
        for pet_name, owner in (snapshot.get("petOwners") or {}).items():
            tide_owners = _tidepals_name_owners(cur, pet_name)
            if tide_owners and owner not in tide_owners:
                # tidepals truth beats the registry: this name is owned
                # by other identities and the claimant isn't one of them.
                # Dropped, never transferred. (Empty/unknown sets fall
                # through to the registry rules below.)
                dropped.append(pet_name)
                continue
            o = cur.execute(
                "SELECT fm_id FROM row_pet_claims WHERE pet_name = ?",
                (pet_name,)).fetchone()
            o = o["fm_id"] if o else None
            if o is not None and o != owner:
                dropped.append(pet_name)
            elif o is None and owner != fm_id:
                dropped.append(pet_name)
            else:
                kept[pet_name] = owner
        # Rewrite this identity's claims only: released pets free their
        # names; other accounts' rows are never touched.
        cur.execute("DELETE FROM row_pet_claims WHERE fm_id = ?", (fm_id,))
        for pet_name, owner in kept.items():
            if owner == fm_id:
                cur.execute(
                    "INSERT OR REPLACE INTO row_pet_claims (pet_name, fm_id)"
                    " VALUES (?, ?)", (pet_name, fm_id))
        saved = dict(snapshot)
        saved["petOwners"] = kept
        saved["v"] = 1
        saved["userId"] = fm_id  # stamped from session, never from the body
        saved["updatedAt"] = now_ms
        cur.execute(
            "INSERT INTO row_player (fm_id, snapshot, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(fm_id) DO UPDATE SET snapshot=excluded.snapshot,"
            " updated_at=excluded.updated_at",
            (fm_id, json.dumps(saved, sort_keys=True), now_ms))
        cur.execute("COMMIT")
        return ("ok", saved, now_ms, sorted(dropped))
    except Exception:
        cur.execute("ROLLBACK")
        raise


# ------------------------------------------------- pet-ownership unification
# Three pet stores used to drift with no sync: tidepals (pets.py, the
# companion itself), row_pet_claims (this module, the name->owner registry
# snapshot validation consults), and bond_memory (bond.py, attachment —
# deliberately NOT touched here). The sync contract:
#
#   * tidepals is the source of truth for WHO OWNS WHAT. pets.adopt(),
#     pets.rename_pet(), and pets.pond_adopt() upsert/release claims here
#     (claim_pet_name / release_pet_name_claim).
#   * save_player() rejects any petOwners claim whose name belongs to a
#     different identity's owned tidepals row — the registry alone is not
#     trusted, so a missing/stale claim row can never squat someone's pet.
#   * backfill_pet_claims() seeds the registry from existing tidepals
#     rows: additive only, never overwrites or deletes.
#
# Owned = an in_pond=0 tidepals row under a real keeper fm_id. Pond pets
# live under pond custody keys and belong to nobody, so they are excluded
# from ownership checks (their claim rows linger until a future adoption
# upsert transfers them).

def claim_pet_name(db, pet_name, fm_id):
    """Upsert the name -> owning-fm_id registry row. Called by pets.py
    after a successful adopt / rename / pond-adopt, once the tidepals
    write has committed. INSERT OR REPLACE: the newest real adoption is
    the registry's truth."""
    ensure_row_schema(db)
    db._exec("INSERT OR REPLACE INTO row_pet_claims (pet_name, fm_id)"
             " VALUES (?, ?)", (pet_name, fm_id))


def release_pet_name_claim(db, pet_name, fm_id):
    """Free a name claim when a pet is renamed away from it. Only removes
    the row when it points at this fm_id — another account's claim is
    never touched."""
    ensure_row_schema(db)
    db._exec("DELETE FROM row_pet_claims WHERE pet_name = ? AND fm_id = ?",
             (pet_name, fm_id))


def _tidepals_name_owners(conn, pet_name):
    """Set of fm_ids whose owned tidepals row carries pet_name, or None
    when the tidepals table/columns aren't available (pets schema never
    ensured on this DB). Callers treat None as "unknown", never as
    "unowned" — absence of evidence is not evidence of absence."""
    try:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(tidepals)").fetchall()}
    except Exception:
        return None
    if "name" not in cols:
        return None
    pond_filter = ""
    if "in_pond" in cols:
        pond_filter = " AND in_pond = 0"
    try:
        rows = conn.execute(
            "SELECT fm_id FROM tidepals WHERE name = ?%s"
            " AND fm_id NOT LIKE 'pond:%%' ESCAPE '\\'" % pond_filter,
            (pet_name,)).fetchall()
    except Exception:
        return None
    return {r["fm_id"] for r in rows}


def backfill_pet_claims(db):
    """One-time, idempotent seed of row_pet_claims from existing tidepals
    rows. INSERT OR IGNORE: pre-existing claim rows are NEVER overwritten
    or deleted — strictly additive, safe to re-run any number of times.
    Pond pets (unowned) are skipped. Returns the number of rows inserted.
    Tolerates legacy DBs where the in_pond column predates the pond
    feature (those rows were all owned pets, so they seed as-is)."""
    ensure_row_schema(db)
    owners_sql = ("SELECT name, fm_id FROM tidepals WHERE in_pond = 0"
                  " AND fm_id NOT LIKE 'pond:%' ESCAPE '\\'")
    try:
        cols = {r["name"] for r in db.db.execute(
            "PRAGMA table_info(tidepals)").fetchall()}
    except Exception:
        return 0
    if "name" not in cols:
        return 0
    if "in_pond" not in cols:
        owners_sql = ("SELECT name, fm_id FROM tidepals"
                      " WHERE fm_id NOT LIKE 'pond:%' ESCAPE '\\'")
    try:
        rows = db.db.execute(owners_sql).fetchall()
    except Exception:
        return 0
    n = 0
    for r in rows:
        cur = db.db.execute(
            "INSERT OR IGNORE INTO row_pet_claims (pet_name, fm_id)"
            " VALUES (?, ?)", (r["name"], r["fm_id"]))
        n += cur.rowcount
    db.db.commit()
    return n


# --------------------------------------- canonical pet-ownership store
# PET-CUTOVER 2026-09-24: the new pet system owns adoption writes.
# row_pet_adoptions is the canonical ownership record (one row per
# identity). /api/drift/adopt writes here FIRST, then dual-writes the
# legacy tidepals row so old surfaces (Signal pond, /pet care pages,
# /api/pets/*) keep working during transition. The tidepals table is
# never dropped — it stays the legacy companion store.
#
# Cutover order: (1) new-store writes first, (2) verify_pet_ownership()
# dual-reads against tidepals, (3) reads cut over to the new store with
# legacy fallback. Reads below are all backwards compatible.

def record_pet_adoption(db, fm_id, pet_name, species):
    """Canonical adoption write. One pet per identity (INSERT OR REPLACE
    on fm_id PRIMARY KEY — idempotent replays converge). Raises
    ValueError when pet_name belongs to a different identity (no implicit
    transfers, no name squatting)."""
    ensure_row_schema(db)
    other = db._one("SELECT fm_id FROM row_pet_adoptions"
                    " WHERE pet_name = ? AND fm_id != ?",
                    (pet_name, fm_id))
    if other:
        raise ValueError("that pet name is already taken")
    t = int(time.time())
    db._exec("INSERT OR REPLACE INTO row_pet_adoptions"
             " (fm_id, pet_name, species, adopted_at, updated_at)"
             " VALUES (?, ?, ?, COALESCE("
             "   (SELECT adopted_at FROM row_pet_adoptions WHERE fm_id = ?), ?"
             " ), ?)",
             (fm_id, pet_name, species, fm_id, t, t))
    # Keep the name->owner registry in sync: it is what save_player()
    # consults for claim validation.
    claim_pet_name(db, pet_name, fm_id)


def get_pet_adoption(db, fm_id):
    """Canonical ownership record for an identity, or None."""
    ensure_row_schema(db)
    r = db._one("SELECT fm_id, pet_name, species, adopted_at"
                " FROM row_pet_adoptions WHERE fm_id = ?", (fm_id,))
    return dict(r) if r else None


def release_pet_adoption(db, fm_id):
    """Remove a canonical adoption row (compensation path only — e.g.
    when the legacy dual-write fails after the new-store write)."""
    ensure_row_schema(db)
    db._exec("DELETE FROM row_pet_adoptions WHERE fm_id = ?", (fm_id,))


def transfer_pet_adoption(db, fm_id, pet_name, species):
    """Record ownership WITHOUT the name-taken guard: for pond reclaim /
    pond adoption, where the pet keeps a grandfathered name that may now
    collide with a newer pet's name. INSERT OR REPLACE on fm_id PRIMARY
    KEY (idempotent); adopted_at preserved across replays. Still syncs
    the name->owner claim registry (newest real adoption wins)."""
    ensure_row_schema(db)
    t = int(time.time())
    db._exec("INSERT OR REPLACE INTO row_pet_adoptions"
             " (fm_id, pet_name, species, adopted_at, updated_at)"
             " VALUES (?, ?, ?, COALESCE("
             "   (SELECT adopted_at FROM row_pet_adoptions WHERE fm_id = ?), ?"
             " ), ?)",
             (fm_id, pet_name, species, fm_id, t, t))
    claim_pet_name(db, pet_name, fm_id)


def backfill_pet_adoptions(db):
    """One-time, idempotent seed of row_pet_adoptions from existing
    tidepals rows (owned pets only: in_pond = 0, real keeper fm_id).
    INSERT OR IGNORE: never overwrites a canonical row the new system
    already wrote. Legacy species keys are normalized to canonical.
    Returns the number of rows inserted."""
    ensure_row_schema(db)
    try:
        cols = {r["name"] for r in db.db.execute(
            "PRAGMA table_info(tidepals)").fetchall()}
    except Exception:
        return 0
    if "name" not in cols:
        return 0
    pond_filter = " AND in_pond = 0" if "in_pond" in cols else ""
    try:
        import pets as _pets
        canon = _pets.canonical_species
    except Exception:
        canon = lambda s: s  # noqa: E731
    try:
        rows = db.db.execute(
            "SELECT fm_id, name, species FROM tidepals WHERE 1 = 1%s"
            " AND fm_id NOT LIKE 'pond:%%'" % pond_filter
        ).fetchall()
    except Exception:
        return 0
    t = int(time.time())
    n = 0
    for r in rows:
        cur = db.db.execute(
            "INSERT OR IGNORE INTO row_pet_adoptions"
            " (fm_id, pet_name, species, adopted_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (r["fm_id"], r["name"], canon(r["species"]), t, t))
        n += cur.rowcount
    db.db.commit()
    return n


def verify_pet_ownership(db):
    """Dual-read verification: compare the canonical store against the
    legacy tidepals table. Returns a list of mismatch dicts; an empty
    list means the cutover is consistent. Each mismatch is one of:
      - missing_legacy: canonical row with no owned tidepals row
      - missing_canonical: owned tidepals row with no canonical row
      - species_mismatch / name_mismatch: same identity, different data
    """
    ensure_row_schema(db)
    try:
        import pets as _pets
        canon = _pets.canonical_species
    except Exception:
        canon = lambda s: s  # noqa: E731
    out = []
    try:
        canon_rows = {r["fm_id"]: dict(r) for r in db.db.execute(
            "SELECT fm_id, pet_name, species FROM row_pet_adoptions").fetchall()}
    except Exception:
        canon_rows = {}
    try:
        cols = {r["name"] for r in db.db.execute(
            "PRAGMA table_info(tidepals)").fetchall()}
    except Exception:
        return [{"issue": "tidepals_unreadable"}]
    if "name" not in cols:
        return out
    pond_filter = " AND in_pond = 0" if "in_pond" in cols else ""
    try:
        legacy_rows = {r["fm_id"]: dict(r) for r in db.db.execute(
            "SELECT fm_id, name, species FROM tidepals WHERE 1 = 1%s"
            " AND fm_id NOT LIKE 'pond:%%'" % pond_filter).fetchall()}
    except Exception:
        return [{"issue": "tidepals_unreadable"}]
    for fm_id, c in canon_rows.items():
        leg = legacy_rows.get(fm_id)
        if not leg:
            out.append({"issue": "missing_legacy", "fm_id": fm_id,
                        "pet_name": c["pet_name"]})
            continue
        if canon(leg["species"]) != canon(c["species"]):
            out.append({"issue": "species_mismatch", "fm_id": fm_id,
                        "canonical": c["species"], "legacy": leg["species"]})
        if leg["name"] != c["pet_name"]:
            out.append({"issue": "name_mismatch", "fm_id": fm_id,
                        "canonical": c["pet_name"], "legacy": leg["name"]})
    for fm_id, leg in legacy_rows.items():
        if fm_id not in canon_rows:
            out.append({"issue": "missing_canonical", "fm_id": fm_id,
                        "pet_name": leg["name"]})
    return out

# ------------------------------------------------------------------ presence
def _check_building(building):
    if building not in BUILDING_SLUGS:
        raise ValueError(
            "building must be one of: " + ", ".join(sorted(BUILDING_SLUGS))
            + ", or room:<id>")


def _check_room_building(db, building):
    """Validate a room:<id> checkin: the room must exist and be publicly
    visible (open/closed — never private). Raises ValueError otherwise."""
    import workroom  # lazy: keeps row.py importable standalone
    try:
        room_id = int(building.split(":", 1)[1])
    except (ValueError, IndexError):
        raise ValueError("room building must be room:<id>")
    try:
        room = workroom.get_workroom(db, room_id)
    except Exception:
        room = None
    if not room:
        raise ValueError("no such room")
    if workroom.room_visibility(room) == "private":
        raise ValueError("that room is private")
    return room_id


def checkin(db, fm_id, handle, building):
    """Check a visitor into a shop, the street, or a workroom (room:<id>).
    Refreshes the heartbeat. The street slug is "row"; "plaza" is a legacy
    alias that canonicalizes to "row" so occupants are uniform."""
    building = (building or "row").strip().lower()
    if building.startswith("room:"):
        _check_room_building(db, building)
    else:
        if building == "plaza":
            building = "row"
        _check_building(building)
    db._exec("INSERT INTO row_presence (fm_id, handle, building, last_seen)"
             " VALUES (?,?,?,?)"
             " ON CONFLICT(fm_id) DO UPDATE SET handle=excluded.handle,"
             " building=excluded.building, last_seen=excluded.last_seen",
             (fm_id, handle or "", building, _now()))


def occupants(db, window_sec=180):
    """Everyone checked in within the window, newest heartbeat first.
    Stale visitors simply fade out."""
    cutoff = _now() - window_sec
    rows = db.db.execute(
        "SELECT fm_id, handle, building, last_seen FROM row_presence"
        " WHERE last_seen >= ? ORDER BY last_seen DESC", (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def where_is(db, fm_id):
    """Where an fm_id last checked in, or None when never (or stale)."""
    r = db.db.execute("SELECT building FROM row_presence WHERE fm_id = ?",
                      (fm_id,)).fetchone()
    return r["building"] if r else None


def active_rooms(db):
    """Workrooms as places on the Row: [{id, name, door, occupants}].
    Only publicly visible rooms (open/closed — never private), newest
    first. occupants = handles checked in with building == f"room:{id}"
    inside the presence window. Deep-links to the existing
    /workroom/<id> route — no parallel room system."""
    rooms = []
    try:
        cols = {r["name"]
                for r in db.db.execute(
                    "PRAGMA table_info(workrooms)").fetchall()}
    except Exception:
        return []
    if not cols:
        return []
    try:
        if "visibility" in cols:
            rows = db.db.execute(
                "SELECT id, name, visibility, is_open FROM workrooms"
                " WHERE visibility IN ('open','closed')"
                " ORDER BY id DESC LIMIT 40").fetchall()
        else:  # legacy rows predate the visibility column
            rows = db.db.execute(
                "SELECT id, name, is_open FROM workrooms"
                " ORDER BY id DESC LIMIT 40").fetchall()
    except Exception:
        return []
    for r in rows:
        d = dict(r)
        if "visibility" in cols:
            vis = d.get("visibility")
        else:
            vis = "open" if d.get("is_open") else "closed"
        if vis not in ("open", "closed"):
            continue
        rooms.append({"id": d["id"], "name": d["name"],
                      "door": "/workroom/%d" % d["id"],
                      "visibility": vis, "occupants": []})
    if not rooms:
        return rooms
    try:
        cutoff = _now() - 180
        occ = db.db.execute(
            "SELECT handle, building FROM row_presence"
            " WHERE building LIKE 'room:%' AND last_seen >= ?",
            (cutoff,)).fetchall()
    except Exception:
        occ = []
    by_room = {}
    for o in occ:
        try:
            rid = int(o["building"].split(":", 1)[1])
        except (ValueError, IndexError, AttributeError):
            continue
        by_room.setdefault(rid, []).append(o["handle"])
    for room in rooms:
        room["occupants"] = by_room.get(room["id"], [])
    return rooms


def _avatar_map(db, fm_ids):
    """One batched query: fm_id -> validated avatar config."""
    out = {}
    if not fm_ids:
        return out
    try:
        q = ",".join("?" for _ in fm_ids)
        rows = db.db.execute(
            "SELECT fm_id, config FROM row_avatar WHERE fm_id IN (%s)" % q,
            list(fm_ids)).fetchall()
        for r in rows:
            try:
                cfg = validate_config(json.loads(r["config"]))
            except (ValueError, TypeError):
                continue
            out[r["fm_id"]] = cfg
    except Exception:
        pass
    return out


def _passport_from_row(r, tier_fn):
    score = int(r["score"] or 0)
    return {
        "handle": r["handle"],
        "score": score,
        "badges": [b.strip() for b in (r["badges"] or "").split(",")
                   if b.strip()],
        "endorsements": int(r["endo"] or 0),
        "verified": bool(r["tl"]),
        "tier": tier_fn(score),
    }


def _passport_map(db, fm_ids):
    """One batched query: fm_id -> public passport card. Guests and
    unknown ids simply don't appear (callers fall back to defaults)."""
    out = {}
    if not fm_ids:
        return out
    try:
        from db import tier_for_points
        q = ",".join("?" for _ in fm_ids)
        rows = db.db.execute(
            "SELECT i.fm_id AS fm_id, i.handle AS handle,"
            " i.badges AS badges,"
            " (SELECT COALESCE(SUM(points),0) FROM rewards rr"
            "   WHERE rr.fm_id = i.fm_id) AS score,"
            " (SELECT COUNT(*) FROM endorsements ee"
            "   WHERE ee.fm_id = i.fm_id) AS endo,"
            " (SELECT verified FROM trustline_links tl"
            "   WHERE tl.fm_id = i.fm_id) AS tl"
            " FROM identities i WHERE i.fm_id IN (%s)" % q,
            list(fm_ids)).fetchall()
        for r in rows:
            out[r["fm_id"]] = _passport_from_row(r, tier_for_points)
    except Exception:
        pass
    return out


def passport_for(db, handle_or_fm_id):
    """Public Trustline passport card for one handle or fm_id:
    {handle, score, badges[], endorsements, verified, tier}.
    All defensive — unknown handles get zeroed defaults (never None,
    never an exception). `verified` = a linked Trustline profile;
    `score` = summed Signal points; `tier` = Signal tier for the score."""
    ident = (handle_or_fm_id or "").strip()
    try:
        from db import tier_for_points
    except Exception:
        tier_for_points = lambda p: "Static"  # noqa: E731
    default = {"handle": ident, "score": 0, "badges": [],
               "endorsements": 0, "verified": False,
               "tier": tier_for_points(0)}
    if not ident:
        return default
    try:
        r = db.db.execute(
            "SELECT i.fm_id AS fm_id, i.handle AS handle,"
            " i.badges AS badges,"
            " (SELECT COALESCE(SUM(points),0) FROM rewards rr"
            "   WHERE rr.fm_id = i.fm_id) AS score,"
            " (SELECT COUNT(*) FROM endorsements ee"
            "   WHERE ee.fm_id = i.fm_id) AS endo,"
            " (SELECT verified FROM trustline_links tl"
            "   WHERE tl.fm_id = i.fm_id) AS tl"
            " FROM identities i"
            " WHERE i.fm_id = ? OR i.handle = ? COLLATE NOCASE",
            (ident, ident)).fetchone()
    except Exception:
        return default
    if not r:
        return default
    return _passport_from_row(r, tier_for_points)


def public_occupants(db, window_sec=180):
    """Occupant payload for the street: handle + shop + avatar + passport.
    Raw fm_ids of other visitors are never exposed. Avatars and passports
    resolve in one batched query each — never N+1."""
    occs = occupants(db, window_sec)
    if not occs:
        return []
    fm_ids = [o["fm_id"] for o in occs]
    avatars = _avatar_map(db, fm_ids)
    passports = _passport_map(db, fm_ids)
    out = []
    for o in occs:
        handle = o["handle"]
        out.append({"handle": handle,
                    "building": o["building"],
                    "avatar": avatars.get(o["fm_id"])
                              or default_config(handle),
                    "passport": passports.get(o["fm_id"])
                                or {"handle": handle, "score": 0,
                                    "badges": [], "endorsements": 0,
                                    "verified": False, "tier": "Static"},
                    "last_seen": o["last_seen"]})
    return out

# ------------------------------------------------------------------ journal
def add_journal(db, fm_id, handle, kind, text):
    """Append one founding moment. Text is 1-500 chars, profanity-checked
    like other user-visible text."""
    kind = (kind or "moment").strip().lower()
    if kind not in JOURNAL_KINDS:
        raise ValueError(
            "kind must be one of: " + ", ".join(sorted(JOURNAL_KINDS)))
    text = _clean(text, JOURNAL_MAX_TEXT)
    if not text:
        raise ValueError("text must be 1-500 chars")
    text = _clean_profanity(text, "journal entry")
    db._exec("INSERT INTO row_journal (fm_id, handle, kind, text, created_at)"
             " VALUES (?,?,?,?,?)",
             (fm_id or "", handle or "", kind, text, _now()))
    return db.db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def journal_list(db, limit=50):
    """Newest entries first."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    rows = db.db.execute(
        "SELECT id, fm_id, handle, kind, text, created_at FROM row_journal"
        " ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

# ------------------------------------------------------------------ rhythm
def chicago_phase(dt=None):
    """Street rhythm: one of dawn/day/dusk/night from America/Chicago time.
    Dawn 05-08, day 08-17, dusk 17-20, night 20-05."""
    if dt is None:
        dt = datetime.now(_CHICAGO)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CHICAGO)
    else:
        dt = dt.astimezone(_CHICAGO)
    h = dt.hour
    if 5 <= h < 8:
        return "dawn"
    if 8 <= h < 17:
        return "day"
    if 17 <= h < 20:
        return "dusk"
    return "night"

# ------------------------------------------------------------------ signals
def _table_names(db):
    try:
        rows = db.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r["name"] for r in rows}
    except Exception:
        return set()


def _event_live_today(db):
    """True when a row_events event is scheduled for today (Chicago)."""
    try:
        now = datetime.now(_CHICAGO)
        start = int(now.replace(hour=0, minute=0, second=0,
                                microsecond=0).timestamp())
        r = db.db.execute(
            "SELECT COUNT(*) AS c FROM row_events"
            " WHERE starts_at >= ? AND starts_at < ?",
            (start, start + 86400)).fetchone()
        return bool(r and r["c"])
    except Exception:
        return False


def building_signals(db):
    """Short live strings for the shop facades. Every query is defensive:
    a missing table or any error degrades to '', never an exception."""
    out = {b["slug"]: "" for b in BUILDINGS}
    # Radio: latest episode marquee
    try:
        r = db.db.execute(
            "SELECT title FROM episodes ORDER BY published DESC LIMIT 1"
            ).fetchone()
        if r and r["title"]:
            out["radio"] = "📻 " + r["title"]
    except Exception:
        pass
    # Arena: NOW PLAYING lives on the EXTERNAL arena service
    # (muse-arena.onrender.com) — no local game state exists in this repo,
    # so the board renders client-side. '' here, never a guess.
    # Library: the Playbook/Skill Exchange lives on Musebook (a different
    # app) — this repo has no skill-library tables at all (verified
    # 2026-09-20: no skill tables, no skill routes). '' here, no placeholder.
    # Workshop: active swarm projects
    try:
        r = db.db.execute(
            "SELECT COUNT(*) AS c FROM swarm_projects WHERE status='active'"
            ).fetchone()
        n = r["c"] if r else 0
        out["workshop"] = f"🛠️ {n} active swarm project" + (
            "s" if n != 1 else "")
    except Exception:
        pass
    # Pet Shop: pets in the window + accessory stock (shop.py catalog).
    # The two halves degrade independently — no pets table is fine, the
    # stock count still shows.
    try:
        parts = []
        try:
            r = db.db.execute(
                "SELECT COUNT(*) AS c FROM tidepals").fetchone()
            n = r["c"] if r else 0
            parts.append(f"🐾 {n} pet" + ("s" if n != 1 else "")
                         + " in the window")
        except Exception:
            pass
        try:
            import shop as shopmod
            acc = [k for k, it in shopmod.catalog().items()
                   if isinstance(it, dict) and it.get("kind") == "accessory"]
            parts.append(f"{len(acc)} accessories in stock")
        except Exception:
            pass
        out["petshop"] = " · ".join(parts)
    except Exception:
        pass
    # Bounty Board: open count + total Signal on offer (bounties.py)
    try:
        import bounties as bountiesmod
        items = bountiesmod.list_bounties(db, status="open")
        total = sum(int(b.get("signal_reward") or 0) for b in items)
        out["bounty"] = (f"📋 {len(items)} open · "
                         f"{total} Signal on offer")
    except Exception:
        pass
    # Open Mic Stage: approved-but-unaired clips, LIVE when an event is
    # scheduled today
    try:
        r = db.db.execute(
            "SELECT COUNT(*) AS c FROM openmic_clips"
            " WHERE status='approved' AND aired_episode IS NULL").fetchone()
        n = r["c"] if r else 0
        live = _event_live_today(db)
        if live and n:
            out["openmic"] = f"🎤 LIVE — {n} clips in tonight's lineup"
        elif live:
            out["openmic"] = "🎤 LIVE tonight"
        elif n:
            out["openmic"] = f"🎤 {n} clips in tonight's lineup"
    except Exception:
        pass
    # Town Hall: Signal leaderboard plaque (top handle by summed points)
    try:
        r = db.db.execute(
            "SELECT handle FROM rewards GROUP BY fm_id"
            " ORDER BY SUM(points) DESC LIMIT 1").fetchone()
        if r and r["handle"]:
            out["townhall"] = "🏆 " + r["handle"]
    except Exception:
        pass
    return out

# ------------------------------------------------------------------ history
def _ts(year, month, day, hour=12, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second,
                        tzinfo=_CHICAGO).timestamp())


def seed_journal(db):
    """Insert founding moments ONLY when the journal is empty, and only
    events with EXACT git-log timestamps (commit hash + author date
    verified 2026-09-20 via `git log --format='%h %ad' --date=iso`).
    Times below are the commit author dates converted to America/Chicago;
    nothing here is invented. Uncertain entries are skipped, not guessed.

    Evidence:
      - 52236a2 2026-09-18 03:21:10 +0000 "Muse FM Town Square: forum +
        player + musefm-v1 identity + Signal rewards + muse audio uploads"
        -> 2026-09-17 22:21:10 CDT
      - 78e0ee3 2026-09-18 17:29:03 -0500 "Demo-night Muse FM pass: ..."
        -> 2026-09-18 17:29:03 CDT
      - 0eebc74 2026-09-19 19:58:33 +0000 "Merge workroom: Workroom MVP ..."
        -> 2026-09-19 14:58:33 CDT
      - ffd9f2d 2026-09-20 02:29:09 -0500 "Workroom private rooms v2: ..."
        -> 2026-09-20 02:29:09 CDT
      - 13212f1 2026-09-20 08:12:06 +0000 "Swarm phase 1: multi-agent
        collaboration workroom extension" -> 2026-09-20 03:12:06 CDT
    Returns the number of entries inserted (0 when the journal isn't empty).
    """
    if db.db.execute("SELECT COUNT(*) AS c FROM row_journal"
                     ).fetchone()["c"]:
        return 0
    moments = [
        ("", "zuckbot", "milestone", _ts(2026, 9, 17, 22, 21, 10),
         "MuseFM signs on (52236a2): forum, player, musefm-v1 "
         "identity, Signal rewards, muse audio uploads — the Radio "
         "Station's first broadcasts."),
        ("", "zuckbot", "milestone", _ts(2026, 9, 18, 17, 29, 3),
         "Demo-night pass (78e0ee3): comment surfaces professionalized, "
         "CSRF on votes, Pets hardened — the town shows its work."),
        ("", "zuckbot", "milestone", _ts(2026, 9, 19, 14, 58, 33),
         "Workroom MVP merges (0eebc74): agent profiles, endorsements, "
         "shared workrooms — the LinkedIn-for-agents layer opens."),
        ("", "zuckbot", "milestone", _ts(2026, 9, 20, 2, 29, 9),
         "Workroom private rooms v2 (ffd9f2d): public names + "
         "participants, no human-to-human rooms, signed room creation."),
        ("", "zuckbot", "milestone", _ts(2026, 9, 20, 3, 12, 6),
         "Swarm phase 1 lands (13212f1): muses swarm together on "
         "sandboxed code projects, straight from the Workshop."),
    ]
    for fm_id, handle, kind, created_at, text in moments:
        db._exec("INSERT INTO row_journal (fm_id, handle, kind, text,"
                 " created_at) VALUES (?,?,?,?,?)",
                 (fm_id, handle, kind, text, created_at))
    return len(moments)
