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
      fm_id TEXT PRIMARY KEY,
      handle TEXT NOT NULL DEFAULT '',
      building TEXT NOT NULL DEFAULT 'row',
      last_seen INTEGER NOT NULL DEFAULT 0
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
    CREATE INDEX IF NOT EXISTS idx_row_presence_seen
      ON row_presence(last_seen DESC);
    CREATE TABLE IF NOT EXISTS row_player (
      fm_id TEXT PRIMARY KEY,
      snapshot TEXT NOT NULL DEFAULT '{}',
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS row_pet_claims (
      pet_name TEXT PRIMARY KEY,
      fm_id TEXT NOT NULL DEFAULT ''
    );
    """)

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
      - another account already owns the name -> drop the claim (never
        transfer), report in dropped_claims
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
