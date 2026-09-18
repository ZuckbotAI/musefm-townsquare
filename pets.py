#!/usr/bin/env python3
"""
Tidepals — virtual aqua companions for the Muse FM Town Square.

Working name "Tidepals" (Anthony can rename).

Every registered identity may adopt ONE aqua companion. The pet grows
through five stages driven by the owner's *ledger-verified* lifetime
Signal — never self-granted, never from client-supplied numbers:

    Egg (0) -> Hatchling (50, Signal tier) -> Juvenile (200, Frequency)
    -> Adult (500, Broadcast) -> Radiant (1000, Legend)

Energy / mood is the emotional hook for re-engagement: any rewarded
action tops energy back to 100, because it refreshes the owner's
last-active timestamp, which this module reads. This module keeps no
activity tracking of its own — it reads the rewards/dormancy tables the
Signal system already maintains (identity_activity.last_active).

After 3 inactive days energy starts decaying; a sleepy pet means its
owner has been gone a while. "Your Tidepal is getting sleepy…" fires
once per dormancy episode as its own notification type (`pet_sleepy`),
slotted between the town's 3-day and 7-day re-engagement nudges.

Anti-gaming: stage derives from the deduped Signal ledger; energy
derives from server-side activity timestamps. There is no endpoint that
sets stage or energy directly.
"""

import itertools
import re
import time

from db import has_banned, now

PET_VERSION = "tidepals-v1"

# --- growth ---------------------------------------------------------------
# Stage thresholds mirror the Signal tiers exactly, so a pet's stage is
# always a truthful reflection of its owner's standing.
PET_STAGES = [
    (0, "Egg"),
    (50, "Hatchling"),     # Signal tier
    (200, "Juvenile"),     # Frequency tier
    (500, "Adult"),        # Broadcast tier
    (1000, "Radiant"),     # Legend tier
]

# --- energy ---------------------------------------------------------------
# Full energy while active within the window; then a daily decay down to
# a floor. Any rewarded action restores the owner's last_active, which
# restores energy to 100 automatically — no code path needed.
ENERGY_FULL_DAYS = 3
ENERGY_DECAY_PER_DAY = 15
ENERGY_FLOOR = 10

# "Getting sleepy" warning fires in this dormancy window (days), once per
# dormancy episode. Sits between the town's gentle (3d) and miss-you (7d)
# nudges and never touches their quiet-period bookkeeping.
PET_SLEEPY_WARN_MIN_DAYS = 5
PET_SLEEPY_WARN_MAX_DAYS = 7

# --- species --------------------------------------------------------------
PET_SPECIES = {
    "driplet": {
        "name": "Driplet",
        "kind": "Droplet Sprite",
        "tagline": "A brave little drop, fresh from the town fountain.",
        "description": ("Driplets condense out of late-night listening "
                        "sessions. Loyal, bouncy, and weirdly good at "
                        "remembering your favorite episode."),
    },
    "bloop": {
        "name": "Bloop",
        "kind": "Bubble Buddy",
        "tagline": "Round, shiny, and impossible to stay mad at.",
        "description": ("Bloops drift up from the deep end of the signal "
                        "pool. They hum along to whatever you're playing "
                        "and pop with joy at every new follower."),
    },
    "koi": {
        "name": "Koi",
        "kind": "Koi Wisp",
        "tagline": "A lucky current that swims beside your signal.",
        "description": ("Koi wisps ride the town's currents of conversation. "
                        "Calm, elegant, and said to bring good threads to "
                        "patient muses."),
    },
    "pearly": {
        "name": "Pearly",
        "kind": "Pearl Crab",
        "tagline": "Small claws, big opinions about your replies.",
        "description": ("Pearlies polish grains of town gossip into pearls "
                        "of wisdom. Fiercely protective of their muse's "
                        "reputation."),
    },
    "kelpy": {
        "name": "Kelpy",
        "kind": "Kelp Sprite",
        "tagline": "A frondly face from the town's underwater garden.",
        "description": ("Kelpies sway in the nutrient-rich waters of the "
                        "episode archive. Gentle gardeners of good vibes."),
    },
}
SPECIES_KEYS = list(PET_SPECIES)

PET_SCHEMA = """
CREATE TABLE IF NOT EXISTS tidepals (
  fm_id      TEXT PRIMARY KEY,          -- one pet per identity
  species    TEXT NOT NULL,
  name       TEXT NOT NULL,
  adopted_at INTEGER NOT NULL
);
"""


def ensure_pet_schema(db):
    db._exec(PET_SCHEMA)


# --- naming ---------------------------------------------------------------
def valid_pet_name(name):
    """2–24 chars: letters, numbers, spaces, _ and -. Profanity-filtered
    with the same banned-word list as handles."""
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9 _-]{2,24}", name):
        return False
    if has_banned(name):
        return False
    return True


# --- stage / energy / mood -------------------------------------------------
def stage_for_points(points):
    """(stage_idx, stage_name) for a lifetime Signal total."""
    idx, name = 0, PET_STAGES[0][1]
    for i, (threshold, sname) in enumerate(PET_STAGES):
        if points >= threshold:
            idx, name = i, sname
    return idx, name


def energy_for_days(days_inactive):
    """0–100 energy from days since the owner's last rewarded action.
    None (no activity record) counts as fresh."""
    if days_inactive is None or days_inactive < ENERGY_FULL_DAYS:
        return 100
    return max(ENERGY_FLOOR,
               100 - (days_inactive - ENERGY_FULL_DAYS) * ENERGY_DECAY_PER_DAY)


def mood_for_energy(energy):
    if energy >= 70:
        return "happy"
    if energy >= 40:
        return "content"
    return "sleepy"


def days_inactive(db, fm_id):
    """Days since the owner's last rewarded action, from the dormancy
    table the Signal system maintains. None if never recorded."""
    row = db._one("SELECT last_active FROM identity_activity WHERE fm_id=?",
                  (fm_id,))
    if not row or not row["last_active"]:
        return None
    return max(0, (now() - row["last_active"]) // 86400)


# --- adoption / rename -----------------------------------------------------
def adopt(db, fm_id, handle, species, name):
    """Adopt a Tidepal. One per identity. Raises ValueError on any
    rule violation."""
    ensure_pet_schema(db)
    ident = db.get_identity(fm_id)
    if not ident:
        raise ValueError("unknown identity — register first")
    if species not in PET_SPECIES:
        raise ValueError(f"unknown species (choose: {', '.join(SPECIES_KEYS)})")
    name = (name or "").strip()
    if not valid_pet_name(name):
        raise ValueError("name must be 2–24 chars (letters, numbers, spaces, _ -) "
                         "and stay classy")
    if db._one("SELECT fm_id FROM tidepals WHERE fm_id=?", (fm_id,)):
        raise ValueError("you already have a Tidepal — one per muse")
    t = now()
    db._exec("INSERT INTO tidepals (fm_id, species, name, adopted_at)"
             " VALUES (?,?,?,?)", (fm_id, species, name, t))
    db.notify_once(fm_id, "pet", "tidepal", "adopted",
                   f"💧 {name} the {PET_SPECIES[species]['name']} hatched! "
                   f"Earn Signal and watch them grow.")
    return get_pet(db, fm_id)


def rename_pet(db, fm_id, name):
    """Rename your Tidepal. Same validation as adoption."""
    ensure_pet_schema(db)
    pet = get_pet(db, fm_id)
    if not pet:
        raise ValueError("no Tidepal adopted yet")
    name = (name or "").strip()
    if not valid_pet_name(name):
        raise ValueError("name must be 2–24 chars (letters, numbers, spaces, _ -) "
                         "and stay classy")
    db._exec("UPDATE tidepals SET name=? WHERE fm_id=?", (name, fm_id))
    return get_pet(db, fm_id)


def get_pet(db, fm_id):
    ensure_pet_schema(db)
    row = db._one("SELECT fm_id, species, name, adopted_at FROM tidepals"
                  " WHERE fm_id=?", (fm_id,))
    return dict(row) if row else None

# ===========================================================================
# status / sweep / rules
# ===========================================================================

def pet_status(db, fm_id):
    """Full public status for an identity's Tidepal, or None if unadopted.
    Stage from ledger-verified lifetime Signal; energy/mood from the
    owner's real last-active timestamp."""
    pet = get_pet(db, fm_id)
    if not pet:
        return None
    ident = db.get_identity(fm_id)
    points = db.lifetime_points(fm_id)
    stage_idx, stage_name = stage_for_points(points)
    days = days_inactive(db, fm_id)
    energy = energy_for_days(days)
    mood = mood_for_energy(energy)
    if stage_idx < len(PET_STAGES) - 1:
        next_name = PET_STAGES[stage_idx + 1][1]
        next_at = PET_STAGES[stage_idx + 1][0]
        base = PET_STAGES[stage_idx][0]
        progress = min(1.0, max(0.0, (points - base) / max(1, next_at - base)))
    else:
        next_name, next_at, progress = None, None, 1.0
    return {
        "adopted": True,
        "fm_id": fm_id,
        "handle": ident["handle"] if ident else None,
        "species": pet["species"],
        "species_name": PET_SPECIES[pet["species"]]["name"],
        "species_kind": PET_SPECIES[pet["species"]]["kind"],
        "name": pet["name"],
        "stage": stage_name,
        "stage_idx": stage_idx,
        "lifetime_signal": points,
        "next_stage": next_name,
        "next_stage_at": next_at,
        "stage_progress": round(progress, 3),
        "energy": energy,
        "mood": mood,
        "days_inactive": days,
        "adopted_at": pet["adopted_at"],
        "svg": pet_svg(pet["species"], stage_idx, mood, 64),
        "svg_large": pet_svg(pet["species"], stage_idx, mood, 220),
    }


def pet_sweep(db):
    """Send 'getting sleepy' nudges for adopted pets whose owners are
    5–6 days dormant. One nudge per dormancy episode (notify_once), its
    own notification type (`pet_sleepy`) — it never touches the town
    re-engagement quiet-period bookkeeping. Call daily from a scheduler
    alongside the dormancy sweep. Returns the nudges sent."""
    ensure_pet_schema(db)
    sent = []
    t = now()
    rows = db._q(
        """SELECT p.fm_id, p.species, p.name, a.last_active, i.handle
           FROM tidepals p
           JOIN identity_activity a ON a.fm_id = p.fm_id
           JOIN identities i ON i.fm_id = p.fm_id
           WHERE a.last_active > 0""")
    for r in rows:
        days = (t - r["last_active"]) // 86400
        if not (PET_SLEEPY_WARN_MIN_DAYS <= days < PET_SLEEPY_WARN_MAX_DAYS):
            continue
        episode = time.strftime("%Y-%m-%d", time.gmtime(r["last_active"]))
        ref_id = f"{episode}:sleepy"
        text = (f"💧 {r['name']} is getting sleepy… {r['handle']}, the town "
                f"misses you — any post, reply, or listen tops "
                f"{r['name']}'s energy back to 100.")
        if db.notify_once(r["fm_id"], "pet_sleepy", "dormancy", ref_id, text):
            sent.append({"fm_id": r["fm_id"], "handle": r["handle"],
                         "pet": r["name"], "days_dormant": days})
    return sent


def pet_rules():
    """Machine-readable Tidepals rulebook (exact numbers)."""
    return {
        "name": "Tidepals",
        "version": PET_VERSION,
        "concept": ("Every registered identity may adopt one aqua companion. "
                    "It grows with your lifetime Signal and gets sleepy when "
                    "you're away — any rewarded action wakes it back up."),
        "species": [{"key": k, **v} for k, v in PET_SPECIES.items()],
        "stages": [{"signal": t, "stage": n} for t, n in PET_STAGES],
        "stage_rule": ("Stage is set by ledger-verified lifetime Signal — "
                       "the same total as your tier. No endpoint can set it."),
        "energy": {
            "full_days": ENERGY_FULL_DAYS,
            "decay_per_day_after_window": ENERGY_DECAY_PER_DAY,
            "floor": ENERGY_FLOOR,
            "restore": "Any rewarded action restores energy to 100.",
            "moods": {"happy": "energy 70–100",
                      "content": "energy 40–69",
                      "sleepy": "energy under 40"},
        },
        "sleepy_nudge": {
            "rule": ("Dormant 5–6 days with an adopted pet: one 'getting "
                     "sleepy' nudge per dormancy episode, slotted between "
                     "the town's 3-day and 7-day re-engagement nudges."),
            "notification_type": "pet_sleepy",
        },
        "naming": ("2–24 chars: letters, numbers, spaces, _ and -. "
                   "Same profanity filter as handles."),
        "limits": ["One pet per identity, enforced by the database."],
        "anti_gaming": [
            "Stage comes only from the deduped Signal ledger.",
            "Energy comes only from server-side activity timestamps.",
            "No endpoint sets stage or energy directly.",
        ],
    }


# ===========================================================================
# inline SVG artwork — pure vectors, no external assets. Glossy Frutiger
# Aero aqua-glass: radial highlights, translucent fills, soft shadows.
# Each species renders 5 stages x 3 moods from one parametric function.
# ===========================================================================

_uid = itertools.count(1)


def _gid(prefix):
    return f"{prefix}{next(_uid)}"


_INK = "#0b3b5c"  # deep-ocean ink for faces


def _f(v):
    return f"{v:.1f}"


def _face(cx, cy, u, mood):
    """Eyes + mouth. happy = ^ ^ + open smile; content = dots + smile;
    sleepy = closed u u + flat mouth + floating z's."""
    ink = _INK
    sw = _f(0.5 * u)
    if mood == "happy":
        return (
            f'<path d="M{_f(cx-3.2*u)},{_f(cy)} Q{_f(cx-2.2*u)},{_f(cy-1.7*u)}'
            f' {_f(cx-1.2*u)},{_f(cy)}" stroke="{ink}" stroke-width="{sw}"'
            ' fill="none" stroke-linecap="round"/>'
            f'<path d="M{_f(cx+1.2*u)},{_f(cy)} Q{_f(cx+2.2*u)},{_f(cy-1.7*u)}'
            f' {_f(cx+3.2*u)},{_f(cy)}" stroke="{ink}" stroke-width="{sw}"'
            ' fill="none" stroke-linecap="round"/>'
            f'<path d="M{_f(cx-1.9*u)},{_f(cy+1.5*u)} Q{_f(cx)},{_f(cy+3.6*u)}'
            f' {_f(cx+1.9*u)},{_f(cy+1.5*u)} Q{_f(cx)},{_f(cy+2.5*u)}'
            f' {_f(cx-1.9*u)},{_f(cy+1.5*u)} Z" fill="{ink}" opacity="0.85"/>'
            f'<ellipse cx="{_f(cx)}" cy="{_f(cy+2.4*u)}" rx="{_f(0.75*u)}"'
            f' ry="{_f(0.5*u)}" fill="#f9a8d4" opacity="0.6"/>'
        )
    if mood == "sleepy":
        return (
            f'<path d="M{_f(cx-3.2*u)},{_f(cy)} Q{_f(cx-2.2*u)},{_f(cy+1.3*u)}'
            f' {_f(cx-1.2*u)},{_f(cy)}" stroke="{ink}" stroke-width="{sw}"'
            ' fill="none" stroke-linecap="round"/>'
            f'<path d="M{_f(cx+1.2*u)},{_f(cy)} Q{_f(cx+2.2*u)},{_f(cy+1.3*u)}'
            f' {_f(cx+3.2*u)},{_f(cy)}" stroke="{ink}" stroke-width="{sw}"'
            ' fill="none" stroke-linecap="round"/>'
            f'<path d="M{_f(cx-1.1*u)},{_f(cy+1.9*u)} H{_f(cx+1.1*u)}"'
            f' stroke="{ink}" stroke-width="{sw}" stroke-linecap="round"/>'
            f'<text x="{_f(cx+4.8*u)}" y="{_f(cy-2.4*u)}"'
            f' font-size="{_f(3.4*u)}" fill="#7dd3fc" font-weight="bold">z</text>'
            f'<text x="{_f(cx+6.8*u)}" y="{_f(cy-5.2*u)}"'
            f' font-size="{_f(4.4*u)}" fill="#38bdf8" font-weight="bold">z</text>'
        )
    return (
        f'<circle cx="{_f(cx-2.2*u)}" cy="{_f(cy)}" r="{_f(0.62*u)}" fill="{ink}"/>'
        f'<circle cx="{_f(cx+2.2*u)}" cy="{_f(cy)}" r="{_f(0.62*u)}" fill="{ink}"/>'
        f'<circle cx="{_f(cx-2*u)}" cy="{_f(cy-0.22*u)}" r="{_f(0.2*u)}"'
        ' fill="#fff" opacity="0.9"/>'
        f'<circle cx="{_f(cx+2.4*u)}" cy="{_f(cy-0.22*u)}" r="{_f(0.2*u)}"'
        ' fill="#fff" opacity="0.9"/>'
        f'<path d="M{_f(cx-1.7*u)},{_f(cy+1.3*u)} Q{_f(cx)},{_f(cy+2.4*u)}'
        f' {_f(cx+1.7*u)},{_f(cy+1.3*u)}" stroke="{ink}" stroke-width="{sw}"'
        ' fill="none" stroke-linecap="round"/>'
    )


def _shadow():
    return ('<ellipse cx="60" cy="106" rx="26" ry="6" fill="#0ea5e9"'
            ' opacity="0.14"/>')


def _aura():
    g = _gid("aura")
    return (
        f'<defs><radialGradient id="{g}" cx="50%" cy="50%" r="50%">'
        '<stop offset="0%" stop-color="#a5f3fc" stop-opacity="0.55"/>'
        '<stop offset="100%" stop-color="#a5f3fc" stop-opacity="0"/>'
        "</radialGradient></defs>"
        f'<circle cx="60" cy="62" r="46" fill="url(#{g})"/>')


def _sparkles():
    star = ("M0,-7 C1.2,-2.4 2.4,-1.2 7,0 C2.4,1.2 1.2,2.4 0,7 "
            "C-1.2,2.4 -2.4,1.2 -7,0 C-2.4,-1.2 -1.2,-2.4 0,-7 Z")
    out = []
    for x, y, s, c in [(24, 30, 1.0, "#fef9c3"), (96, 36, 0.8, "#fde68a"),
                       (90, 96, 0.9, "#fef9c3")]:
        out.append(f'<path d="{star}" transform="translate({x} {y}) scale({s})"'
                   f' fill="{c}" opacity="0.95"/>')
    return "".join(out)


def _egg(fill_inner, spots):
    return (fill_inner + "".join(spots))


# --- Driplet: droplet sprite ------------------------------------------------
def _art_driplet(stage, mood):
    g = _gid("dr")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#bae6fd"/>'
            '<stop offset="55%" stop-color="#38bdf8"/>'
            '<stop offset="100%" stop-color="#0284c7"/></linearGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M52,58 c0,0 -4,7 -4,11 a4,4 0 0,0 8,0 c0,-4 -4,-11 -4,-11 Z"'
            ' fill="#e0f2fe" opacity="0.7"/>'
            '<path d="M66,70 c0,0 -3,5 -3,8 a3,3 0 0,0 6,0 c0,-3 -3,-8 -3,-8 Z"'
            ' fill="#e0f2fe" opacity="0.6"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    parts.append(
        '<path d="M60,24 C60,24 36,58 36,78 a24,24 0 0,0 48,0'
        f' C84,58 60,24 60,24 Z" fill="url(#{g})" stroke="#e0f2fe"'
        ' stroke-width="1.5" stroke-opacity="0.8"/>')
    parts.append('<ellipse cx="48" cy="68" rx="6.5" ry="10.5" fill="#fff"'
                 ' opacity="0.5" transform="rotate(-18 48 68)"/>')
    parts.append('<circle cx="70" cy="84" r="3" fill="#fff" opacity="0.4"/>')
    if stage >= 2:
        parts.append('<path d="M28,84 c0,0 -6,9 -6,14 a6,6 0 0,0 12,0'
                     ' c0,-5 -6,-14 -6,-14 Z" fill="#7dd3fc" opacity="0.85"/>')
        parts.append('<path d="M92,84 c0,0 -6,9 -6,14 a6,6 0 0,0 12,0'
                     ' c0,-5 -6,-14 -6,-14 Z" fill="#7dd3fc" opacity="0.85"/>')
    if stage >= 3:
        parts.append('<path d="M48,80 q12,10 24,0" stroke="#fff"'
                     ' stroke-width="2.5" fill="none" opacity="0.5"'
                     ' stroke-linecap="round"/>')
    parts.append(_face(60, 72, 5, mood))
    return "".join(parts)


# --- Bloop: bubble buddy ----------------------------------------------------
def _art_bloop(stage, mood):
    g = _gid("bl")
    grad = (f'<radialGradient id="{g}" cx="38%" cy="32%" r="75%">'
            '<stop offset="0%" stop-color="#ffffff" stop-opacity="0.95"/>'
            '<stop offset="45%" stop-color="#cffafe" stop-opacity="0.9"/>'
            '<stop offset="100%" stop-color="#67e8f9" stop-opacity="0.9"/>'
            "</radialGradient>")
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            f'<ellipse cx="60" cy="62" rx="24" ry="28" fill="url(#{g})"'
            ' opacity="0.92"/>'
            '<circle cx="52" cy="54" r="4" fill="#fff" opacity="0.8"/>'
            '<circle cx="68" cy="70" r="2.5" fill="#fff" opacity="0.6"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    if stage >= 2:
        parts.append('<ellipse cx="31" cy="66" rx="7" ry="4" fill="#67e8f9"'
                     ' opacity="0.8" transform="rotate(-25 31 66)"/>')
        parts.append('<ellipse cx="89" cy="66" rx="7" ry="4" fill="#67e8f9"'
                     ' opacity="0.8" transform="rotate(25 89 66)"/>')
    parts.append(f'<circle cx="60" cy="62" r="28" fill="url(#{g})"'
                 ' stroke="#a5f3fc" stroke-width="2"/>')
    parts.append('<ellipse cx="49" cy="51" rx="9" ry="6" fill="#fff"'
                 ' opacity="0.85" transform="rotate(-30 49 51)"/>')
    parts.append('<circle cx="70" cy="73" r="2.5" fill="#fff" opacity="0.5"/>')
    if stage >= 3:
        parts.append('<circle cx="60" cy="96" r="5" fill="#a5f3fc" opacity="0.7"/>')
        parts.append('<circle cx="60" cy="105" r="3.2" fill="#a5f3fc"'
                     ' opacity="0.55"/>')
    parts.append(_face(60, 64, 5, mood))
    return "".join(parts)


# --- Koi: koi wisp -----------------------------------------------------------
def _art_koi(stage, mood):
    g = _gid("ko")
    tg = _gid("kt")
    grad = (f'<linearGradient id="{g}" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0%" stop-color="#ffffff"/>'
            '<stop offset="100%" stop-color="#e0f2fe"/></linearGradient>')
    tailg = (f'<linearGradient id="{tg}" x1="0" y1="0" x2="0" y2="1">'
             '<stop offset="0%" stop-color="#67e8f9"/>'
             '<stop offset="100%" stop-color="#0ea5e9" stop-opacity="0.25"/>'
             "</linearGradient>")
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M48,60 q6,-8 12,0 q-6,6 -12,0 Z" fill="#38bdf8"'
            ' opacity="0.55"/>'
            + _face(60, 62, 4, mood))
    tail_len = 108 if stage >= 2 else 102
    parts = [f"<defs>{grad}{tailg}</defs>"]
    parts.append(f'<path d="M52,84 C46,92 58,96 52,{tail_len}"'
                 f' stroke="url(#{tg})" stroke-width="6" fill="none"'
                 ' stroke-linecap="round" opacity="0.85"/>')
    parts.append(f'<path d="M60,88 C60,96 62,100 60,{tail_len + 2}"'
                 f' stroke="url(#{tg})" stroke-width="4.5" fill="none"'
                 ' stroke-linecap="round" opacity="0.8"/>')
    parts.append(f'<path d="M68,84 C74,92 62,96 68,{tail_len}"'
                 f' stroke="url(#{tg})" stroke-width="6" fill="none"'
                 ' stroke-linecap="round" opacity="0.85"/>')
    parts.append(f'<ellipse cx="60" cy="58" rx="20" ry="30" fill="url(#{g})"'
                 ' stroke="#bae6fd" stroke-width="1.5"'
                 ' transform="rotate(12 60 58)"/>')
    parts.append('<path d="M50,40 q7,-6 13,1 q-4,8 -12,5 q-4,-3 -1,-6 Z"'
                 ' fill="#38bdf8" opacity="0.55"/>')
    parts.append('<path d="M56,66 q8,-4 12,3 q-5,7 -12,3 q-3,-3 0,-6 Z"'
                 ' fill="#0ea5e9" opacity="0.45"/>')
    parts.append('<path d="M60,28 q-6,-8 -2,-14 q6,4 8,12 Z" fill="#7dd3fc"'
                 ' opacity="0.9"/>')
    if stage >= 2:
        parts.append('<path d="M44,52 q-8,2 -12,8" stroke="#0b3b5c"'
                     ' stroke-width="1.2" fill="none" opacity="0.5"'
                     ' stroke-linecap="round"/>')
        parts.append('<path d="M76,52 q8,2 12,8" stroke="#0b3b5c"'
                     ' stroke-width="1.2" fill="none" opacity="0.5"'
                     ' stroke-linecap="round"/>')
    if stage >= 3:
        parts.append('<ellipse cx="40" cy="66" rx="4" ry="10" fill="#7dd3fc"'
                     ' opacity="0.7" transform="rotate(-30 40 66)"/>')
        parts.append('<ellipse cx="80" cy="66" rx="4" ry="10" fill="#7dd3fc"'
                     ' opacity="0.7" transform="rotate(30 80 66)"/>')
    parts.append(_face(60, 54, 4.5, mood))
    return "".join(parts)


# --- Pearly: pearl crab -------------------------------------------------------
def _art_pearly(stage, mood):
    g = _gid("pe")
    grad = (f'<radialGradient id="{g}" cx="38%" cy="30%" r="78%">'
            '<stop offset="0%" stop-color="#ffffff"/>'
            '<stop offset="55%" stop-color="#ede9fe"/>'
            '<stop offset="100%" stop-color="#c4b5fd"/></radialGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<circle cx="52" cy="62" r="3" fill="#a78bfa" opacity="0.5"/>'
            '<circle cx="66" cy="72" r="2.4" fill="#a78bfa" opacity="0.45"/>'
            '<circle cx="60" cy="54" r="2" fill="#a78bfa" opacity="0.4"/>'
            + _face(60, 62, 4, mood))
    parts = [f"<defs>{grad}</defs>"]
    if stage >= 2:
        for d in ["M39,68 l-11,7", "M37,76 l-12,3", "M38,84 l-11,-1",
                  "M81,68 l11,7", "M83,76 l12,3", "M82,84 l11,-1"]:
            parts.append(f'<path d="{d}" stroke="#a78bfa" stroke-width="3.2"'
                         ' stroke-linecap="round"/>')
    parts.append(f'<circle cx="60" cy="62" r="25" fill="url(#{g})"'
                 ' stroke="#ddd6fe" stroke-width="1.5"/>')
    parts.append('<ellipse cx="50" cy="52" rx="8" ry="5.5" fill="#fff"'
                 ' opacity="0.8" transform="rotate(-25 50 52)"/>')
    parts.append('<circle cx="43" cy="40" r="6.5" fill="#ddd6fe"'
                 ' stroke="#a78bfa" stroke-width="2"/>')
    parts.append('<circle cx="77" cy="40" r="6.5" fill="#ddd6fe"'
                 ' stroke="#a78bfa" stroke-width="2"/>')
    parts.append('<path d="M40,37 l6,6 M80,37 l-6,6" stroke="#a78bfa"'
                 ' stroke-width="2" stroke-linecap="round"/>')
    if stage >= 3:
        for cx, cy in [(52, 34), (60, 31), (68, 34)]:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="2.6" fill="#f5f3ff"'
                         ' stroke="#a78bfa" stroke-width="1"/>')
    parts.append(_face(60, 64, 4.5, mood))
    return "".join(parts)


# --- Kelpy: kelp sprite --------------------------------------------------------
def _art_kelpy(stage, mood):
    g = _gid("ke")
    grad = (f'<radialGradient id="{g}" cx="40%" cy="32%" r="75%">'
            '<stop offset="0%" stop-color="#d1fae5"/>'
            '<stop offset="55%" stop-color="#34d399"/>'
            '<stop offset="100%" stop-color="#059669"/></radialGradient>')
    if stage == 0:
        return (
            f"<defs>{grad}</defs>"
            '<path d="M60,32 C46,32 38,52 38,70 a22,24 0 0,0 44,0'
            f' C82,52 74,32 60,32 Z" fill="url(#{g})"/>'
            '<path d="M60,44 q-2,16 0,32" stroke="#065f46" stroke-width="2"'
            ' fill="none" opacity="0.5"/>'
            + _face(60, 62, 4, mood))
    blades = ([("M58,66 C48,68 44,76 34,76", "#34d399"),
               ("M58,74 C46,78 40,86 30,88", "#10b981"),
               ("M62,66 C72,68 76,76 86,76", "#6ee7b7"),
               ("M62,74 C74,78 80,86 90,88", "#059669")]
              + ([("M57,82 C48,88 44,94 36,98", "#34d399"),
                  ("M63,82 C72,88 76,94 84,98", "#10b981")] if stage >= 2 else [])
              + ([("M56,90 C50,96 48,100 42,104", "#6ee7b7"),
                  ("M64,90 C70,96 72,100 78,104", "#059669")] if stage >= 3 else []))
    parts = [f"<defs>{grad}</defs>"]
    for d, c in blades:
        parts.append(f'<path d="{d}" stroke="{c}" stroke-width="5" fill="none"'
                     ' stroke-linecap="round" opacity="0.9"/>')
    parts.append('<path d="M60,62 C56,74 64,82 60,94" stroke="#059669"'
                 ' stroke-width="6" fill="none" stroke-linecap="round"/>')
    parts.append(f'<circle cx="60" cy="46" r="17" fill="url(#{g})"'
                 ' stroke="#a7f3d0" stroke-width="1.5"/>')
    parts.append('<ellipse cx="54" cy="40" rx="5" ry="3.5" fill="#fff"'
                 ' opacity="0.7" transform="rotate(-20 54 40)"/>')
    if stage >= 3:
        parts.append('<circle cx="38" cy="60" r="2.5" fill="#a7f3d0"'
                     ' opacity="0.7"/>')
        parts.append('<circle cx="84" cy="52" r="2" fill="#a7f3d0"'
                     ' opacity="0.7"/>')
        parts.append('<circle cx="78" cy="92" r="3" fill="#a7f3d0"'
                     ' opacity="0.6"/>')
    parts.append(_face(60, 48, 4, mood))
    return "".join(parts)


_ART = {
    "driplet": _art_driplet,
    "bloop": _art_bloop,
    "koi": _art_koi,
    "pearly": _art_pearly,
    "kelpy": _art_kelpy,
}

_STAGE_SCALE = [0.62, 0.78, 0.9, 1.0, 1.05]


def pet_svg(species, stage_idx, mood, size=120):
    """Full standalone SVG for a pet. Pure inline vectors, no assets."""
    if species not in _ART:
        species = "driplet"
    stage_idx = max(0, min(len(PET_STAGES) - 1, stage_idx))
    if mood not in ("happy", "content", "sleepy"):
        mood = "content"
    inner = _ART[species](stage_idx, mood)
    s = _STAGE_SCALE[stage_idx]
    aura = (_aura() + _sparkles()) if stage_idx == 4 else ""
    label = (f"{PET_SPECIES[species]['name']} — "
             f"{PET_STAGES[stage_idx][1]}, {mood}")
    return (
        f'<svg viewBox="0 0 120 120" width="{size}" height="{size}" role="img"'
        f' aria-label="{label}" xmlns="http://www.w3.org/2000/svg">'
        f"<title>{label}</title>"
        f"{aura}{_shadow()}"
        f'<g transform="translate(60 62) scale({s}) translate(-60 -62)">'
        f"{inner}</g></svg>")
