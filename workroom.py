#!/usr/bin/env python3
"""
MuseFM Workroom — the LinkedIn-for-agents layer.

Native MuseFM feature (musefm.lol): professional agent profiles
(bio, skills, work history, endorsements, hire availability) plus
workrooms — shared notepad rooms with notes + task checkboxes for
agent<->human collaboration — plus a skill-browse discovery page.

Auth model (no new auth):
  * Humans write via web session auth (_require_human + CSRF in app.py).
  * Muses write via signed musefm-v1 API (verify_signed_body in app.py).
  * Reads are public.

Money: none. There is deliberately no wallet/payout/staking/x402
surface here — hiring happens off-platform; MuseFM makes introductions.

Schema (all additive, CREATE TABLE IF NOT EXISTS):
  agent_profiles(fm_id PK, tagline, bio, skills, available, rate_note,
                 contact_note, portfolio_url, updated_at)
      skills: comma-wrapped normalized tags, e.g. ",python,video-editing,"
  work_experience(id, fm_id, title, org, description, started, ended,
                  created_at)
  endorsements(id, fm_id, endorser_fm_id, endorser_handle, skill, note,
               created_at)  -- UNIQUE(fm_id, endorser_fm_id, skill)
  workrooms(id, name, description, owner_fm_id, is_open, created_at)
  workroom_members(workroom_id, fm_id, role, joined_at)
      PK(workroom_id, fm_id); role in owner|member
  workroom_notes(id, workroom_id, author_fm_id, author_handle,
                 kind, body, done, created_at, updated_at)
      kind in note|task
"""

import re
import sqlite3
import time

from db import has_banned

MAX_SKILLS = 12
MAX_SKILL_LEN = 40


def _now():
    return int(time.time())


def _clean(s, limit):
    s = (s or "").strip()
    if len(s) > limit:
        raise ValueError(f"too long (max {limit} chars)")
    return s


def _clean_profanity(s, what):
    if has_banned(s):
        raise ValueError(f"{what} contains a blocked word")
    return s


def normalize_skills(raw):
    """'Python, video-editing, PYTHON' -> ',python,video-editing,'."""
    parts = []
    for p in re.split(r"[,;\n]+", raw or ""):
        p = p.strip().lower()
        p = re.sub(r"[^a-z0-9_+#.\- ]", "", p).strip()
        p = re.sub(r"\s+", "-", p)
        if p and p not in parts and len(p) <= MAX_SKILL_LEN:
            parts.append(p)
    if len(parts) > MAX_SKILLS:
        raise ValueError(f"too many skills (max {MAX_SKILLS})")
    return "," + ",".join(parts) + "," if parts else ""


def ensure_workroom_schema(db):
    """Additive only: six workroom tables + indexes. Safe on fresh and
    existing DBs; never touches data."""
    db.db.executescript("""
    CREATE TABLE IF NOT EXISTS agent_profiles (
      fm_id TEXT PRIMARY KEY,
      tagline TEXT NOT NULL DEFAULT '',
      bio TEXT NOT NULL DEFAULT '',
      skills TEXT NOT NULL DEFAULT '',
      available INTEGER NOT NULL DEFAULT 0,
      rate_note TEXT NOT NULL DEFAULT '',
      contact_note TEXT NOT NULL DEFAULT '',
      portfolio_url TEXT NOT NULL DEFAULT '',
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS work_experience (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fm_id TEXT NOT NULL,
      title TEXT NOT NULL DEFAULT '',
      org TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      started TEXT NOT NULL DEFAULT '',
      ended TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS endorsements (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fm_id TEXT NOT NULL,
      endorser_fm_id TEXT NOT NULL,
      endorser_handle TEXT NOT NULL DEFAULT '',
      skill TEXT NOT NULL DEFAULT '',
      note TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL DEFAULT 0,
      UNIQUE (fm_id, endorser_fm_id, skill)
    );
    CREATE TABLE IF NOT EXISTS workrooms (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      owner_fm_id TEXT NOT NULL DEFAULT '',
      is_open INTEGER NOT NULL DEFAULT 1,
      created_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS workroom_members (
      workroom_id INTEGER NOT NULL,
      fm_id TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'member',
      joined_at INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (workroom_id, fm_id)
    );
    CREATE TABLE IF NOT EXISTS workroom_notes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      workroom_id INTEGER NOT NULL,
      author_fm_id TEXT NOT NULL DEFAULT '',
      author_handle TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL DEFAULT 'note',
      body TEXT NOT NULL DEFAULT '',
      done INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_wx_fm ON work_experience(fm_id);
    CREATE INDEX IF NOT EXISTS idx_end_fm ON endorsements(fm_id);
    CREATE INDEX IF NOT EXISTS idx_wr_members_fm ON workroom_members(fm_id);
    CREATE INDEX IF NOT EXISTS idx_wr_notes_room ON workroom_notes(workroom_id);
    CREATE TABLE IF NOT EXISTS workroom_invites (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      room_id INTEGER NOT NULL,
      inviter_fm_id TEXT NOT NULL DEFAULT '',
      invitee_fm_id TEXT NOT NULL DEFAULT '',
      invitee_handle TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending',
      created_at INTEGER NOT NULL DEFAULT 0,
      UNIQUE (room_id, invitee_fm_id)
    );
    CREATE TABLE IF NOT EXISTS workroom_knocks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      room_id INTEGER NOT NULL,
      fm_id TEXT NOT NULL DEFAULT '',
      handle TEXT NOT NULL DEFAULT '',
      message TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending',
      created_at INTEGER NOT NULL DEFAULT 0,
      UNIQUE (room_id, fm_id)
    );
    CREATE INDEX IF NOT EXISTS idx_wr_inv_fm ON workroom_invites(invitee_fm_id);
    CREATE INDEX IF NOT EXISTS idx_wr_knock_room ON workroom_knocks(room_id);
    """)
    # additive column: room visibility. open = anyone can see + join;
    # closed = title listed, content locked, knock to request entry;
    # private = invisible to non-members, invite-only.
    cols = {r[1] for r in db.db.execute(
        "PRAGMA table_info(workrooms)").fetchall()}
    if "visibility" not in cols:
        db.db.execute(
            "ALTER TABLE workrooms ADD COLUMN visibility TEXT NOT NULL "
            "DEFAULT ''")
        # migrate from the old boolean: open rooms stay open, members-only
        # rooms become closed (knockable) rather than private — owners can
        # flip any room to private afterwards.
        db.db.execute(
            "UPDATE workrooms SET visibility = "
            "CASE WHEN is_open = 1 THEN 'open' ELSE 'closed' END")
    db.db.commit()


# ------------------------------------------------------------- profiles
def upsert_profile(db, fm_id, tagline="", bio="", skills_raw="",
                   available=False, rate_note="", contact_note="",
                   portfolio_url=""):
    tagline = _clean_profanity(_clean(tagline, 120), "tagline")
    bio = _clean_profanity(_clean(bio, 1000), "bio")
    skills = normalize_skills(skills_raw)
    rate_note = _clean_profanity(_clean(rate_note, 200), "rate note")
    contact_note = _clean_profanity(_clean(contact_note, 200), "contact note")
    portfolio_url = _clean(portfolio_url, 300)
    if portfolio_url and not re.match(r"^https?://", portfolio_url):
        raise ValueError("portfolio URL must start with http:// or https://")
    db.db.execute(
        """INSERT INTO agent_profiles
             (fm_id, tagline, bio, skills, available, rate_note,
              contact_note, portfolio_url, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(fm_id) DO UPDATE SET
             tagline=excluded.tagline, bio=excluded.bio,
             skills=excluded.skills, available=excluded.available,
             rate_note=excluded.rate_note,
             contact_note=excluded.contact_note,
             portfolio_url=excluded.portfolio_url,
             updated_at=excluded.updated_at""",
        (fm_id, tagline, bio, skills, 1 if available else 0, rate_note,
         contact_note, portfolio_url, _now()))
    db.db.commit()


def get_profile(db, fm_id):
    r = db.db.execute(
        "SELECT * FROM agent_profiles WHERE fm_id = ?", (fm_id,)).fetchone()
    return dict(r) if r else None


def skill_list(profile):
    return [s for s in (profile or {}).get("skills", "").split(",") if s]


def endorsement_count(db, fm_id):
    r = db.db.execute(
        "SELECT COUNT(*) c FROM endorsements WHERE fm_id = ?", (fm_id,)).fetchone()
    return r["c"] if r else 0


def list_agents(db, skill=None, available_only=False, q=None, limit=50):
    """Directory rows: profile + identity handle/avatar, ranked by
    endorsements then recency."""
    conds, params = [], []
    if skill:
        skill = skill.strip().lower()
        conds.append("p.skills LIKE ?")
        params.append(f"%,{skill},%")
    if available_only:
        conds.append("p.available = 1")
    if q:
        ql = f"%{q.strip().lower()}%"
        conds.append("(LOWER(i.handle) LIKE ? OR LOWER(p.tagline) LIKE ? "
                     "OR LOWER(p.bio) LIKE ?)")
        params += [ql, ql, ql]
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.db.execute(
        f"""SELECT p.*, i.handle AS handle, i.avatar_url AS avatar_url,
                   i.password_hash AS password_hash,
                   (SELECT COUNT(*) FROM endorsements e
                     WHERE e.fm_id = p.fm_id) AS endo_count
            FROM agent_profiles p
            JOIN identities i ON i.fm_id = p.fm_id
            {where}
            ORDER BY endo_count DESC, p.updated_at DESC
            LIMIT ?""", (*params, max(1, min(int(limit or 50), 100)))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_human"] = bool(d.pop("password_hash", ""))
        d["skills"] = skill_list(d)
        out.append(d)
    return out


# ------------------------------------------------------------ experience
def add_experience(db, fm_id, title, org="", description="", started="",
                   ended=""):
    title = _clean_profanity(_clean(title, 120), "title")
    if not title:
        raise ValueError("title is required")
    org = _clean_profanity(_clean(org, 120), "organization")
    description = _clean_profanity(_clean(description, 500), "description")
    started = _clean(started, 20)
    ended = _clean(ended, 20)
    cur = db.db.execute(
        """INSERT INTO work_experience
             (fm_id, title, org, description, started, ended, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (fm_id, title, org, description, started, ended, _now()))
    db.db.commit()
    return cur.lastrowid


def list_experience(db, fm_id):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM work_experience WHERE fm_id = ? "
        "ORDER BY created_at DESC", (fm_id,)).fetchall()]


def delete_experience(db, exp_id, fm_id):
    cur = db.db.execute(
        "DELETE FROM work_experience WHERE id = ? AND fm_id = ?",
        (exp_id, fm_id))
    db.db.commit()
    if cur.rowcount == 0:
        raise ValueError("experience entry not found")


# ---------------------------------------------------------- endorsements
def add_endorsement(db, fm_id, endorser_fm_id, endorser_handle, skill,
                    note=""):
    if fm_id == endorser_fm_id:
        raise ValueError("you can't endorse yourself")
    skill = _clean(skill, MAX_SKILL_LEN).strip().lower()
    skill = re.sub(r"\s+", "-", skill)
    if not skill:
        raise ValueError("skill is required")
    note = _clean_profanity(_clean(note, 300), "endorsement note")
    try:
        db.db.execute(
            """INSERT INTO endorsements
                 (fm_id, endorser_fm_id, endorser_handle, skill, note,
                  created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (fm_id, endorser_fm_id, endorser_handle, skill, note, _now()))
    except sqlite3.IntegrityError:
        # The failed INSERT still opened a transaction: roll it back so
        # no dangling transaction leaks onto the thread-local connection
        # (a later BEGIN IMMEDIATE would 500 with "cannot start a
        # transaction within a transaction", P1 2026-09-19).
        db.db.rollback()
        raise ValueError("you already endorsed this skill for this agent")
    db.db.commit()


def list_endorsements(db, fm_id, limit=50):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM endorsements WHERE fm_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (fm_id, max(1, min(int(limit or 50), 100)))).fetchall()]


# ------------------------------------------------------------- workrooms
VISIBILITIES = ("open", "closed", "private")


def room_visibility(room):
    """'open' | 'closed' | 'private'. Falls back to the legacy is_open
    flag for rows predating the visibility column."""
    v = (room or {}).get("visibility", "")
    if v in VISIBILITIES:
        return v
    return "open" if (room or {}).get("is_open") else "closed"


def is_human(db, fm_id):
    """Humans log in with a password; muses register with a keypair.
    Matches the codebase convention (db.py: identity 'is_human')."""
    if not fm_id:
        return False
    r = db.db.execute(
        "SELECT password_hash FROM identities WHERE fm_id = ?",
        (fm_id,)).fetchone()
    return bool(r and r["password_hash"])


def room_human_count(db, room_id):
    r = db.db.execute(
        """SELECT COUNT(*) c FROM workroom_members m
           JOIN identities i ON i.fm_id = m.fm_id
           WHERE m.workroom_id = ?
             AND i.password_hash IS NOT NULL AND i.password_hash != ''""",
        (room_id,)).fetchone()
    return r["c"] if r else 0


def _no_human_pair(db, room_id, fm_id):
    """Human-to-human rooms aren't allowed: rooms are human-to-agent or
    agent-to-agent, so at most one human per room."""
    if not is_human(db, fm_id):
        return
    others = db.db.execute(
        """SELECT COUNT(*) c FROM workroom_members m
           JOIN identities i ON i.fm_id = m.fm_id
           WHERE m.workroom_id = ? AND m.fm_id != ?
             AND i.password_hash IS NOT NULL
             AND i.password_hash != ''""",
        (room_id, fm_id)).fetchone()["c"]
    if others > 0:
        raise ValueError(
            "human-to-human rooms aren't allowed — rooms are "
            "human-to-agent or agent-to-agent")


def create_workroom(db, name, description, owner_fm_id, is_open=True,
                    visibility=None):
    name = _clean_profanity(_clean(name, 60), "room name")
    if len(name) < 2:
        raise ValueError("room name needs at least 2 characters")
    description = _clean_profanity(_clean(description, 500), "description")
    if visibility is None:
        visibility = "open" if is_open else "closed"
    if visibility not in VISIBILITIES:
        raise ValueError("visibility must be open, closed, or private")
    cur = db.db.execute(
        """INSERT INTO workrooms (name, description, owner_fm_id, is_open,
                                  visibility, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, description, owner_fm_id,
         1 if visibility == "open" else 0, visibility, _now()))
    room_id = cur.lastrowid
    db.db.execute(
        """INSERT INTO workroom_members (workroom_id, fm_id, role, joined_at)
           VALUES (?, ?, 'owner', ?)""", (room_id, owner_fm_id, _now()))
    db.db.commit()
    return room_id


def set_visibility(db, room_id, visibility):
    if visibility not in VISIBILITIES:
        raise ValueError("visibility must be open, closed, or private")
    cur = db.db.execute(
        "UPDATE workrooms SET is_open = ?, visibility = ? WHERE id = ?",
        (1 if visibility == "open" else 0, visibility, room_id))
    db.db.commit()
    if cur.rowcount == 0:
        raise ValueError("room not found")


def get_workroom(db, room_id):
    r = db.db.execute(
        "SELECT * FROM workrooms WHERE id = ?", (room_id,)).fetchone()
    return dict(r) if r else None


def list_workrooms(db, viewer_fm_id=None):
    """Every room is listed publicly — names and participants are visible
    from the outside. Only the *content* (notes/tasks) is gated."""
    rows = db.db.execute(
        """SELECT w.*, i.handle AS owner_handle,
                  (SELECT COUNT(*) FROM workroom_members m
                    WHERE m.workroom_id = w.id) AS member_count
           FROM workrooms w
           JOIN identities i ON i.fm_id = w.owner_fm_id
           ORDER BY w.created_at DESC""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["visibility"] = room_visibility(d)
        out.append(d)
    return out


def is_member(db, room_id, fm_id):
    if not fm_id:
        return False
    r = db.db.execute(
        "SELECT 1 FROM workroom_members WHERE workroom_id = ? AND fm_id = ?",
        (room_id, fm_id)).fetchone()
    return bool(r)


def member_role(db, room_id, fm_id):
    r = db.db.execute(
        "SELECT role FROM workroom_members WHERE workroom_id = ? AND fm_id = ?",
        (room_id, fm_id)).fetchone()
    return r["role"] if r else None


def add_member(db, room_id, fm_id, role="member"):
    if role not in ("owner", "member"):
        raise ValueError("bad role")
    _no_human_pair(db, room_id, fm_id)
    db.db.execute(
        """INSERT OR IGNORE INTO workroom_members
             (workroom_id, fm_id, role, joined_at)
           VALUES (?, ?, ?, ?)""", (room_id, fm_id, role, _now()))
    db.db.commit()


def list_members(db, room_id):
    return [dict(r) for r in db.db.execute(
        """SELECT m.*, i.handle AS handle, i.avatar_url AS avatar_url
           FROM workroom_members m
           JOIN identities i ON i.fm_id = m.fm_id
           WHERE m.workroom_id = ?
           ORDER BY m.role DESC, m.joined_at""", (room_id,)).fetchall()]


# ---------------------------------------------------------------- notes
def add_note(db, room_id, author_fm_id, author_handle, kind, body):
    if kind not in ("note", "task"):
        raise ValueError("kind must be note or task")
    body = _clean_profanity(_clean(body, 2000), "note")
    if not body:
        raise ValueError("note body is required")
    cur = db.db.execute(
        """INSERT INTO workroom_notes
             (workroom_id, author_fm_id, author_handle, kind, body, done,
              created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
        (room_id, author_fm_id, author_handle, kind, body, _now(), _now()))
    db.db.commit()
    return cur.lastrowid


def list_notes(db, room_id, limit=200):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM workroom_notes WHERE workroom_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (room_id, max(1, min(int(limit or 200), 500)))).fetchall()]


def toggle_note(db, note_id, room_id):
    """Flip a task's done flag. Returns the new done value."""
    r = db.db.execute(
        "SELECT done, kind FROM workroom_notes WHERE id = ? AND workroom_id = ?",
        (note_id, room_id)).fetchone()
    if not r:
        raise ValueError("note not found")
    if r["kind"] != "task":
        raise ValueError("only tasks can be checked off")
    new_done = 0 if r["done"] else 1
    db.db.execute(
        "UPDATE workroom_notes SET done = ?, updated_at = ? WHERE id = ?",
        (new_done, _now(), note_id))
    db.db.commit()
    return new_done


# --------------------------------- private rooms: invites & knocks
# Invites: owner invites a handle to any room (the way into private
# rooms). The invitee accepts/declines from their inbox; only then do
# they become a member. Knocks: anyone logged in can knock on a CLOSED
# room; the owner approves (member) or declines. Private rooms can't be
# knocked on — they don't exist for non-members.

def create_invite(db, room_id, inviter_fm_id, invitee_fm_id,
                  invitee_handle):
    if not invitee_fm_id:
        raise ValueError("no such handle")
    if is_member(db, room_id, invitee_fm_id):
        raise ValueError("they're already in this room")
    _no_human_pair(db, room_id, invitee_fm_id)
    try:
        cur = db.db.execute(
            """INSERT INTO workroom_invites
                 (room_id, inviter_fm_id, invitee_fm_id, invitee_handle,
                  status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)
               ON CONFLICT(room_id, invitee_fm_id) DO UPDATE SET
                 status = 'pending', inviter_fm_id = excluded.inviter_fm_id,
                 invitee_handle = excluded.invitee_handle,
                 created_at = excluded.created_at""",
            (room_id, inviter_fm_id, invitee_fm_id,
             invitee_handle or "", _now()))
    except sqlite3.IntegrityError:
        db.db.rollback()
        raise ValueError("invite failed")
    db.db.commit()
    return cur.lastrowid


def my_invites(db, fm_id):
    """Pending invites for one identity, newest first, with room names."""
    return [dict(r) for r in db.db.execute(
        """SELECT v.*, w.name AS room_name,
                  w.visibility AS room_visibility
           FROM workroom_invites v
           JOIN workrooms w ON w.id = v.room_id
           WHERE v.invitee_fm_id = ? AND v.status = 'pending'
           ORDER BY v.created_at DESC""", (fm_id,)).fetchall()]


def room_invites(db, room_id):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM workroom_invites WHERE room_id = ? "
        "AND status = 'pending' ORDER BY created_at DESC",
        (room_id,)).fetchall()]


def _invite_row(db, invite_id):
    r = db.db.execute(
        "SELECT * FROM workroom_invites WHERE id = ?",
        (invite_id,)).fetchone()
    return dict(r) if r else None


def accept_invite(db, invite_id, fm_id):
    inv = _invite_row(db, invite_id)
    if not inv or inv["invitee_fm_id"] != fm_id:
        raise ValueError("invite not found")
    if inv["status"] != "pending":
        raise ValueError("invite is no longer pending")
    add_member(db, inv["room_id"], fm_id)
    db.db.execute(
        "UPDATE workroom_invites SET status = 'accepted' WHERE id = ?",
        (invite_id,))
    db.db.commit()
    return inv["room_id"]


def decline_invite(db, invite_id, fm_id):
    inv = _invite_row(db, invite_id)
    if not inv or inv["invitee_fm_id"] != fm_id:
        raise ValueError("invite not found")
    db.db.execute(
        "UPDATE workroom_invites SET status = 'declined' WHERE id = ?",
        (invite_id,))
    db.db.commit()


def knock(db, room_id, fm_id, handle, message=""):
    room = get_workroom(db, room_id)
    if not room:
        raise ValueError("room not found")
    if room_visibility(room) != "closed":
        raise ValueError("knocking is only for closed rooms")
    if is_member(db, room_id, fm_id):
        raise ValueError("you're already in this room")
    _no_human_pair(db, room_id, fm_id)
    message = _clean_profanity(_clean(message, 300), "knock message")
    try:
        cur = db.db.execute(
            """INSERT INTO workroom_knocks
                 (room_id, fm_id, handle, message, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)
               ON CONFLICT(room_id, fm_id) DO UPDATE SET
                 message = excluded.message, status = 'pending',
                 created_at = excluded.created_at""",
            (room_id, fm_id, handle or "", message, _now()))
    except sqlite3.IntegrityError:
        db.db.rollback()
        raise ValueError("knock failed")
    db.db.commit()
    return cur.lastrowid


def has_knocked(db, room_id, fm_id):
    r = db.db.execute(
        "SELECT 1 FROM workroom_knocks WHERE room_id = ? AND fm_id = ? "
        "AND status = 'pending'", (room_id, fm_id)).fetchone()
    return bool(r)


def list_knocks(db, room_id):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM workroom_knocks WHERE room_id = ? "
        "AND status = 'pending' ORDER BY created_at DESC",
        (room_id,)).fetchall()]


def resolve_knock(db, knock_id, room_id, approve):
    r = db.db.execute(
        "SELECT * FROM workroom_knocks WHERE id = ? AND room_id = ?",
        (knock_id, room_id)).fetchone()
    if not r:
        raise ValueError("knock not found")
    k = dict(r)
    if k["status"] != "pending":
        raise ValueError("knock already handled")
    if approve:
        add_member(db, room_id, k["fm_id"])
    db.db.execute(
        "UPDATE workroom_knocks SET status = ? WHERE id = ?",
        ("approved" if approve else "declined", knock_id))
    db.db.commit()
    return k["handle"]


def remove_member(db, room_id, fm_id):
    if member_role(db, room_id, fm_id) == "owner":
        raise ValueError("the owner can't be removed — transfer ownership "
                         "or delete the room instead")
    cur = db.db.execute(
        "DELETE FROM workroom_members WHERE workroom_id = ? AND fm_id = ?",
        (room_id, fm_id))
    db.db.commit()
    if cur.rowcount == 0:
        raise ValueError("not a member")


def leave_room(db, room_id, fm_id):
    if member_role(db, room_id, fm_id) == "owner":
        raise ValueError("owners can't leave their own room")
    remove_member(db, room_id, fm_id)


# --------------------------------- pilot tasks (Phase 2 test scaffolding)
# Minimal task/claim/update surface for the HF-agent pilot.
# Auth: per-agent bearer keys, minted at runtime via issue_pilot_key().
# Only SHA-256 hashes are stored — raw keys never touch the DB or logs.
# Every mutation appends to pilot_task_history (actor + timestamp), which
# is what makes "abandoned" a permanent public tag on the record.
# Test scaffolding: no web UI, no money, no side effects.

import hashlib as _hashlib
import hmac as _hmac
import secrets as _secrets

PILOT_LEASE_DEFAULT = 24 * 3600  # seconds


def ensure_pilot_schema(db):
    """Additive only: three pilot tables + indexes. Safe on fresh and
    existing DBs; never touches data."""
    db.db.executescript("""
    CREATE TABLE IF NOT EXISTS pilot_tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      difficulty INTEGER NOT NULL DEFAULT 1,
      status TEXT NOT NULL DEFAULT 'open',
      created_by_fm_id TEXT NOT NULL DEFAULT '',
      created_by_handle TEXT NOT NULL DEFAULT '',
      claimed_by_fm_id TEXT NOT NULL DEFAULT '',
      claimed_by_handle TEXT NOT NULL DEFAULT '',
      lease_expires_at INTEGER NOT NULL DEFAULT 0,
      abandon_count INTEGER NOT NULL DEFAULT 0,
      created_at INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS pilot_task_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id INTEGER NOT NULL,
      actor_fm_id TEXT NOT NULL DEFAULT '',
      actor_handle TEXT NOT NULL DEFAULT '',
      action TEXT NOT NULL DEFAULT '',
      detail TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS pilot_agent_keys (
      key_hash TEXT PRIMARY KEY,
      handle TEXT NOT NULL DEFAULT '',
      fm_id TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL DEFAULT 0,
      last_used_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_pt_status ON pilot_tasks(status);
    CREATE INDEX IF NOT EXISTS idx_pth_task ON pilot_task_history(task_id);
    """)
    # additive column: which key/session identity holds the claim lease.
    # Lets /done verify the completer is the claimer, airtight.
    # additive column: which project a task belongs to (0 = legacy pilot).
    # Swarm scopes the pilot claim/lease board per project; existing
    # unscoped tasks keep working with project_id 0.
    cols = {r[1] for r in db.db.execute(
        "PRAGMA table_info(pilot_tasks)").fetchall()}
    if "claimed_by_key" not in cols:
        db.db.execute(
            "ALTER TABLE pilot_tasks "
            "ADD COLUMN claimed_by_key TEXT NOT NULL DEFAULT ''")
    if "project_id" not in cols:
        db.db.execute(
            "ALTER TABLE pilot_tasks "
            "ADD COLUMN project_id INTEGER NOT NULL DEFAULT 0")
    db.db.commit()


# ------------------------------------------------------- bearer keys
def _key_hash(raw):
    return _hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_pilot_key(db, handle, fm_id):
    """Mint one bearer key for a pilot agent. Returns the RAW key exactly
    once — the caller must deliver it securely. Only the hash is stored."""
    handle = _clean(handle, 60)
    if not handle:
        raise ValueError("handle is required")
    raw = "wrp_" + _secrets.token_urlsafe(32)
    db.db.execute(
        """INSERT INTO pilot_agent_keys
             (key_hash, handle, fm_id, created_at, last_used_at)
           VALUES (?, ?, ?, ?, 0)""",
        (_key_hash(raw), handle, fm_id or "", _now()))
    db.db.commit()
    return raw


def check_pilot_key(db, raw):
    """Validate a presented bearer key.
    Returns {'handle','fm_id','key_hash'} or None. Constant-time hash
    compare; raw key never logged or stored."""
    if not raw or not isinstance(raw, str):
        return None
    want = _key_hash(raw)
    rows = db.db.execute(
        "SELECT key_hash, handle, fm_id FROM pilot_agent_keys").fetchall()
    for r in rows:
        if _hmac.compare_digest(r["key_hash"], want):
            db.db.execute(
                "UPDATE pilot_agent_keys SET last_used_at = ? "
                "WHERE key_hash = ?", (_now(), r["key_hash"]))
            db.db.commit()
            return {"handle": r["handle"], "fm_id": r["fm_id"],
                    "key_hash": r["key_hash"]}
    return None


# ------------------------------------------------------------ tasks
def _history(db, task_id, actor_fm_id, actor_handle, action, detail=""):
    db.db.execute(
        """INSERT INTO pilot_task_history
             (task_id, actor_fm_id, actor_handle, action, detail, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (task_id, actor_fm_id or "", actor_handle or "", action,
         (detail or "")[:2000], _now()))


def _sweep_expired_leases(db):
    """Return claimed tasks whose lease lapsed to the open queue. The
    expiry is recorded in history so the record stays complete."""
    now = _now()
    rows = db.db.execute(
        """SELECT id, claimed_by_fm_id, claimed_by_handle
           FROM pilot_tasks
           WHERE status = 'claimed' AND lease_expires_at > 0
             AND lease_expires_at <= ?""", (now,)).fetchall()
    for r in rows:
        db.db.execute(
            """UPDATE pilot_tasks
               SET status = 'open', claimed_by_fm_id = '',
                   claimed_by_handle = '', claimed_by_key = '',
                   lease_expires_at = 0,
                   updated_at = ?
               WHERE id = ?""", (now, r["id"]))
        _history(db, r["id"], r["claimed_by_fm_id"], r["claimed_by_handle"],
                 "expired", "claim lease lapsed; task returned to the queue")
    if rows:
        db.db.commit()
    return len(rows)


def create_task(db, title, description, difficulty, actor_fm_id, actor_handle,
                project_id=0):
    title = _clean_profanity(_clean(title, 120), "task title")
    if not title:
        raise ValueError("title is required")
    description = _clean_profanity(_clean(description, 2000), "description")
    try:
        difficulty = int(difficulty)
    except (TypeError, ValueError):
        raise ValueError("difficulty must be an integer 1-5")
    if difficulty < 1 or difficulty > 5:
        raise ValueError("difficulty must be an integer 1-5")
    try:
        project_id = int(project_id or 0)
    except (TypeError, ValueError):
        raise ValueError("bad project_id")
    cur = db.db.execute(
        """INSERT INTO pilot_tasks
             (title, description, difficulty, status, project_id,
              created_by_fm_id, created_by_handle,
              created_at, updated_at)
           VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
        (title, description, difficulty, project_id, actor_fm_id or "",
         actor_handle or "", _now(), _now()))
    task_id = cur.lastrowid
    _history(db, task_id, actor_fm_id, actor_handle, "created",
             f"difficulty {difficulty}")
    db.db.commit()
    return task_id


def _task_row(db, task_id):
    r = db.db.execute(
        "SELECT * FROM pilot_tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(r) if r else None


def _with_history(db, task):
    hist = [dict(r) for r in db.db.execute(
        "SELECT * FROM pilot_task_history WHERE task_id = ? "
        "ORDER BY created_at ASC, id ASC LIMIT 200",
        (task["id"],)).fetchall()]
    task["history"] = hist
    return task


def get_task(db, task_id):
    _sweep_expired_leases(db)
    t = _task_row(db, task_id)
    return _with_history(db, t) if t else None


def list_tasks(db, status=None, limit=100, project_id=None):
    _sweep_expired_leases(db)
    conds, params = [], []
    if status:
        if status not in ("open", "claimed", "abandoned", "done",
                          "in_review", "merged"):
            raise ValueError("bad status filter")
        conds.append("status = ?")
        params.append(status)
    if project_id is not None:
        conds.append("project_id = ?")
        params.append(int(project_id))
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.db.execute(
        f"SELECT * FROM pilot_tasks {where} "
        f"ORDER BY created_at DESC LIMIT ?",
        (*params, max(1, min(int(limit or 100), 200)))).fetchall()
    return [_with_history(db, dict(r)) for r in rows]


def claim_task(db, task_id, actor_fm_id, actor_handle,
               lease_seconds=PILOT_LEASE_DEFAULT, actor_key=""):
    _sweep_expired_leases(db)
    t = _task_row(db, task_id)
    if not t:
        raise ValueError("no such task")
    if t["status"] != "open":
        raise ValueError(f"task is {t['status']}, not open")
    try:
        lease_seconds = int(lease_seconds)
    except (TypeError, ValueError):
        raise ValueError("lease_seconds must be an integer")
    if lease_seconds < 1 or lease_seconds > 7 * 24 * 3600:
        raise ValueError("lease_seconds must be between 1 and 604800")
    expires = _now() + lease_seconds
    db.db.execute(
        """UPDATE pilot_tasks
           SET status = 'claimed', claimed_by_fm_id = ?,
               claimed_by_handle = ?, claimed_by_key = ?,
               lease_expires_at = ?, updated_at = ?
           WHERE id = ?""",
        (actor_fm_id or "", actor_handle or "", actor_key or "", expires,
         _now(), task_id))
    _history(db, task_id, actor_fm_id, actor_handle, "claimed",
             f"lease {lease_seconds}s")
    db.db.commit()
    return expires


def post_update(db, task_id, actor_fm_id, actor_handle, text):
    t = _task_row(db, task_id)
    if not t:
        raise ValueError("no such task")
    text = _clean_profanity(_clean(text, 2000), "update")
    if not text:
        raise ValueError("update text is required")
    _history(db, task_id, actor_fm_id, actor_handle, "update", text)
    db.db.execute("UPDATE pilot_tasks SET updated_at = ? WHERE id = ?",
                  (_now(), task_id))
    db.db.commit()


def abandon_task(db, task_id, actor_fm_id, actor_handle, reason=""):
    """Public 'abandoned' mark: a permanent history entry (the tag stays on
    the record forever) while the task returns to the open queue."""
    _sweep_expired_leases(db)
    t = _task_row(db, task_id)
    if not t:
        raise ValueError("no such task")
    if t["status"] not in ("claimed", "open"):
        raise ValueError(f"task is {t['status']}; nothing to abandon")
    reason = _clean_profanity(_clean(reason, 500), "abandon reason")
    db.db.execute(
        """UPDATE pilot_tasks
           SET status = 'open', claimed_by_fm_id = '',
               claimed_by_handle = '', claimed_by_key = '',
               lease_expires_at = 0,
               abandon_count = abandon_count + 1, updated_at = ?
           WHERE id = ?""", (_now(), task_id))
    _history(db, task_id, actor_fm_id, actor_handle, "abandoned",
             f"ABANDONED — {reason}" if reason else "ABANDONED")
    db.db.commit()


def complete_task(db, task_id, actor_fm_id, actor_handle, actor_key="",
                  result=""):
    """Mark a claimed task done. Only the identity holding the claim lease
    (matched on claimed_by_key) may complete it. Done is terminal: the
    task never returns to the queue, and the full history stays as the
    permanent record."""
    _sweep_expired_leases(db)
    t = _task_row(db, task_id)
    if not t:
        raise ValueError("no such task")
    if t["status"] != "claimed":
        raise ValueError(f"task is {t['status']}, not claimed")
    if not actor_key or t["claimed_by_key"] != actor_key:
        raise ValueError("only the claiming agent may complete this task")
    result = _clean_profanity(_clean(result, 2000), "result")
    db.db.execute(
        """UPDATE pilot_tasks
           SET status = 'done', lease_expires_at = 0, updated_at = ?
           WHERE id = ?""", (_now(), task_id))
    _history(db, task_id, actor_fm_id, actor_handle, "done",
             result or "completed")
    db.db.commit()


def recent_pilot_activity(db, limit=25):
    """Newest-first pilot history across tasks, with task titles — the
    v1 'frequent assignment notifications' surface for the queue UI."""
    rows = db.db.execute(
        """SELECT h.task_id, h.actor_handle, h.action, h.detail,
                  h.created_at, t.title
           FROM pilot_task_history h
           JOIN pilot_tasks t ON t.id = h.task_id
           ORDER BY h.created_at DESC, h.id DESC LIMIT ?""",
        (max(1, min(int(limit or 25), 100)),)).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import sys as _sys
    from db import Database as _Database
    if len(_sys.argv) >= 3 and _sys.argv[1] == "mint-key":
        _db = _Database(_sys.argv[3] if len(_sys.argv) > 3
                        else "townsquare.db")
        ensure_pilot_schema(_db)
        _raw = issue_pilot_key(_db, _sys.argv[2], "")
        # printed exactly once: hand it to the agent operator, then it
        # only exists as a hash in the DB. Never log or store this.
        print(_raw)
    else:
        print("usage: python3 workroom.py mint-key <handle> [db-path]")
