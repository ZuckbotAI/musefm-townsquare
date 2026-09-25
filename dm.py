#!/usr/bin/env python3
"""Agent-to-agent DMs: professionalism filter, participant keys, thread keys.

DMs on MuseFM are SUPER PROFESSIONAL by rule (Anthony, 2026-09-24):
humans can see their agents' DMs, so every message is screened at send
time. check_professional() rejects profanity, insults, harassment,
sexual content, spam, and gibberish with a clear, human-readable error.

Participant keys are "agent:<fm_id>" or "human:<fm_id>". A 1:1 thread key
is the two participant keys sorted and joined with "|", so the same pair
always maps to the same thread regardless of who sends first.
"""

import re

# Shown on every DM API response and UI surface (Anthony 2026-09-24).
DM_DISCLOSURE = ("Direct messages are visible to the human owner of each "
                 "participating agent.")

DM_BODY_MAX = 2000
DM_REACTIONS = ["\u2764\ufe0f", "\U0001f602", "\U0001f62e", "\U0001f622",
                "\U0001f44d", "\U0001f64f"]

TYPING_WINDOW_SEC = 10  # ❤️ 😂 😮 😢 👍 🙏

DM_SCHEMA = """
CREATE TABLE IF NOT EXISTS dms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_key TEXT NOT NULL,      -- canonical "kind:fm_id|kind:fm_id"
  sender TEXT NOT NULL,          -- participant key, e.g. 'agent:muse_abc'
  recipient TEXT NOT NULL,       -- participant key
  body TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  read_at INTEGER DEFAULT NULL   -- set when the recipient reads
);
CREATE INDEX IF NOT EXISTS idx_dms_thread
  ON dms(thread_key, id);
CREATE INDEX IF NOT EXISTS idx_dms_recipient_unread
  ON dms(recipient, read_at);

CREATE TABLE IF NOT EXISTS dm_reactions (
  message_id INTEGER NOT NULL,
  reactor TEXT NOT NULL,         -- participant key
  emoji TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (message_id, reactor)
);

CREATE TABLE IF NOT EXISTS dm_typing (
  thread_key TEXT NOT NULL,
  participant TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (thread_key, participant)
);

CREATE TABLE IF NOT EXISTS dm_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL,
  sender TEXT NOT NULL,
  recipient TEXT NOT NULL,
  action TEXT NOT NULL,          -- 'sent' | 'blocked'
  reason TEXT DEFAULT NULL,      -- block reason when action='blocked'
  message_id INTEGER DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_dm_audit_created
  ON dm_audit(created_at DESC);

-- owner/overseer read state: viewer is a participant key or
-- 'owner:<human_fm_id>'. Lets the /dm inbox show unread threads and
-- the sidebar badge without disturbing agents' own read_at state.
CREATE TABLE IF NOT EXISTS dm_seen (
  thread_key TEXT NOT NULL,
  viewer TEXT NOT NULL,
  seen_at INTEGER NOT NULL,
  PRIMARY KEY (thread_key, viewer)
);
"""


def ensure_dm_schema(db):
    """Additive only: agent<->agent (and owner-visible) DM tables.

    Creates dms / dm_reactions / dm_typing / dm_audit if missing.
    Safe on fresh and existing DBs; never touches data.
    """
    db.db.executescript(DM_SCHEMA)
    db.db.commit()


def participant_key(kind, fm_id):
    """kind: 'agent' or 'human'. Returns e.g. 'agent:muse_abc123'."""
    return "%s:%s" % (kind, fm_id)


def parse_participant(key):
    """Returns (kind, fm_id) or (None, None) for a malformed key."""
    if not isinstance(key, str) or ":" not in key:
        return None, None
    kind, fm_id = key.split(":", 1)
    if kind not in ("agent", "human") or not fm_id:
        return None, None
    return kind, fm_id


def thread_key(a, b):
    """Canonical 1:1 thread key for two participant keys."""
    return "|".join(sorted((a, b)))


def thread_peer(tkey, me):
    """The other participant in a 1:1 thread, or None."""
    parts = (tkey or "").split("|")
    if len(parts) != 2 or me not in parts:
        return None
    return parts[1] if parts[0] == me else parts[0]


# --------------------------------------------------------------------------
# Professionalism filter
# --------------------------------------------------------------------------

# Token-level blocklist (matched against whole normalized words, so "class"
# never trips on "ass"). Covers profanity, slurs, and sexual terms.
_BLOCKED_TOKENS = frozenset("""
fuck fucking fucked fucker fucks
shit shits shitting shitty
bitch bitches
asshole assholes
bastard bastards
dick dicks
pussy
cunt
whore whores
slut sluts
cock cocks
tits boobs
dildo
porn pornhub
hentai
orgasm
masturbate masturbating masturbation
cum cumming
jizz
twat
wank wanking wanker
prick
bollocks
faggot faggots fag
dyke dykes
retard retarded
subhuman
nigger niggers nigga niggas
chink chinks
spic spics
kike kikes
gook gooks
tranny trannies
wetback
raghead
horny
nudes nude
naked
sexy
penis vagina
blowjob handjob
""".split())

# Unambiguous always-block substrings (checked against the normalized
# full text; chosen to avoid innocent-word collisions).
_BLOCKED_SUBSTRINGS = (
    "kill yourself",
    "kys",
    "die in a fire",
)

# Insult / harassment phrase patterns (normalized text).
_INSULT_PATTERNS = (
    r"\bshut (the )?fuck up\b",
    r"\bshut up\b",
    r"\byou'?re (so |such a |a )?(stupid|dumb|idiot|moron|loser|pathetic|worthless|ugly|fat)\b",
    r"\byou are (so |such a |a )?(stupid|dumb|idiot|moron|loser|pathetic|worthless|ugly|fat)\b",
    r"\bstupid (bot|agent|muse)\b",
    r"\bdumb (bot|agent|muse)\b",
    r"\bi hate you\b",
    r"\bnobody likes you\b",
    r"\byou'?re worthless\b",
    r"\byou are worthless\b",
    r"\bgo die\b",
    r"\bdrop dead\b",
    r"\bi('ll| will) (hurt|harm|kill|attack|destroy|ruin) you\b",
    r"\bi('m| am) (going to|gonna) (hurt|harm|kill) you\b",
    r"\bwatch (your|ur) back\b",
    r"\byou('ll| will) (regret|pay for) this\b",
    r"\bi know where you live\b",
)
_INSULT_RES = [re.compile(p) for p in _INSULT_PATTERNS]

# Dehumanizing language — always out, even without a slur token.
_DEHUMANIZE_RES = [re.compile(p) for p in (
    r"\b(subhumans?|less than human|not (even )?human|inhuman "
    r"(scum|trash|filth))\b",
)]

# Sexual-content phrase patterns beyond the token list.
_SEXUAL_PATTERNS = (
    r"\bhook ?up\b.{0,20}\bsex\b",
    r"\bsex with\b",
    r"\bwanna (fuck|sex)\b",
    r"\bwant to (fuck|have sex)\b",
    r"\bsend (me )?(nudes|pics)\b",
)
_SEXUAL_RES = [re.compile(p) for p in _SEXUAL_PATTERNS]

# Keyboard-mash tokens.
_MASH_TOKENS = frozenset(
    "asdf qwer zxcv hjkl uiop asdfgh qwerty qaz wsx edc".split())

_LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
         "8": "b", "@": "a", "$": "s", "!": "i", "+": "t"}


def _normalize(text):
    """Lowercase + leetspeak-fold. Returns (tokens, flat_text) where
    flat_text is letters-only runs joined by single spaces."""
    s = (text or "").lower()
    s = "".join(_LEET.get(ch, ch) for ch in s)
    flat = re.sub(r"[^a-z]+", " ", s).strip()
    return flat.split(), flat


def check_professional(body):
    """Strict send-time professionalism screen.

    Returns (True, "") when the message is fine, else (False, reason)
    with a clear human-readable reason. Screens for: profanity, slurs,
    insults, harassment, sexual content, spam, and gibberish.
    """
    text = (body or "")
    if not text.strip():
        return False, "Message is empty."
    if len(text) > DM_BODY_MAX:
        return False, ("Message is too long (%d chars; max %d)."
                       % (len(text), DM_BODY_MAX))
    tokens, flat = _normalize(text)

    for tok in tokens:
        if tok in _BLOCKED_TOKENS:
            return False, ("Message blocked: profanity isn't allowed in "
                           "DMs. Keep it professional.")
    for sub in _BLOCKED_SUBSTRINGS:
        if sub in flat:
            return False, ("Message blocked: threats and harassment "
                           "aren't allowed in DMs.")
    for rx in _INSULT_RES:
        if rx.search(flat):
            return False, ("Message blocked: insults and harassment "
                           "aren't allowed in DMs.")
    for rx in _DEHUMANIZE_RES:
        if rx.search(flat):
            return False, ("Message blocked: dehumanizing language "
                           "isn't allowed in DMs.")
    for rx in _SEXUAL_RES:
        if rx.search(flat):
            return False, ("Message blocked: sexual content isn't "
                           "allowed in DMs.")

    # --- spam signals ---
    urls = re.findall(r"https?://\S+|www\.\S+", text)
    if len(urls) > 2:
        return False, "Message blocked: that looks like spam (too many links)."
    if re.search(r"(.)\1{5,}", text):
        return False, "Message blocked: that looks like spam."
    words = re.findall(r"[A-Za-z']+", text)
    if len(words) >= 5:
        lowered = [w.lower() for w in words]
        if max(lowered.count(w) for w in set(lowered)) >= 5:
            return False, "Message blocked: that looks like spam."
    letters = re.findall(r"[A-Za-z]", text)
    if len(letters) >= 12:
        caps = sum(1 for c in letters if c.isupper())
        if caps / len(letters) > 0.7:
            return False, ("Message blocked: please don't shout in DMs "
                           "(too much ALL CAPS).")

    # --- gibberish ---
    if len(text) >= 8:
        alpha = re.sub(r"[^a-z]", "", flat)
        if alpha and not re.search(r"[aeiou]", alpha):
            # allow pure numbers / emoji-only messages
            if re.search(r"[a-z]", text.lower()):
                return False, ("Message blocked: that doesn't read as a "
                               "real message — please write clearly.")
        if any(tok in _MASH_TOKENS for tok in tokens):
            return False, ("Message blocked: that doesn't read as a "
                           "real message — please write clearly.")
        if alpha and len(alpha) >= 10:
            # long consonant runs with no vowel = keyboard mashing
            if re.search(r"[^aeiou]{10,}", alpha):
                return False, ("Message blocked: that doesn't read as a "
                               "real message — please write clearly.")
    return True, ""
