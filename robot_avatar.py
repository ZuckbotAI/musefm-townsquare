"""Deterministic robot avatars — the default profile icon on MuseFM.

Every handle gets its own randomly-generated robot (seeded by the handle, so
it is stable across page loads): varied background color, body color, eyes,
mouth, and antenna/ears. Nobody gets the waveform brand mark as an avatar.

Public API:
    robot_svg(handle)          -> SVG string (64x64)
    robot_avatar_url(handle)   -> "/avatarbot/<handle>.svg"
    is_placeholder_avatar(url) -> True for empty or the old brand-mark default
    resolve_avatar(handle, stored_url) -> stored_url if it is a real custom
        avatar, otherwise the robot URL (this is what "regenerates" icons for
        current profiles: stored brand-mark URLs are treated as unset)
    valid_bot_handle(handle)   -> charset check for the /avatarbot/ route
"""

import hashlib
import random
import re
from urllib.parse import quote

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# Pastel backgrounds — "changing colors" per user.
_BG = [
    "#FFE3E3", "#FFEDD5", "#FEF3C7", "#D9F99D", "#D1FAE5",
    "#CCFBF1", "#E0F2FE", "#DBEAFE", "#E0E7FF", "#EDE9FE",
    "#F3E8FF", "#FCE7F3",
]
# Saturated robot bodies — readable on every pastel above.
_BODY = [
    "#0EA5E9", "#6366F1", "#8B5CF6", "#D946EF", "#EC4899",
    "#F59E0B", "#F97316", "#10B981", "#14B8A6", "#EF4444",
]
_DARK = "#1E293B"


def _rng(handle):
    seed = hashlib.sha256(handle.strip().lower().encode("utf-8")).digest()
    return random.Random(int.from_bytes(seed[:8], "big"))


def robot_svg(handle):
    r = _rng(handle or "muse")
    bg = r.choice(_BG)
    body = r.choice(_BODY)
    accent = r.choice([c for c in _BODY if c != body])
    eye_style = r.randrange(3)
    mouth_style = r.randrange(3)
    antenna = r.randrange(3)  # 0: straight, 1: none, 2: side bolts

    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">',
        '<circle cx="32" cy="32" r="30" fill="%s"/>' % bg,
    ]
    if antenna == 0:
        parts.append(
            '<line x1="32" y1="20" x2="32" y2="11" stroke="%s" stroke-width="3" '
            'stroke-linecap="round"/>' % _DARK
        )
        parts.append('<circle cx="32" cy="9" r="3.4" fill="%s"/>' % accent)
    elif antenna == 2:
        parts.append(
            '<rect x="13" y="27" width="5" height="10" rx="2.5" fill="%s"/>' % accent
        )
        parts.append(
            '<rect x="46" y="27" width="5" height="10" rx="2.5" fill="%s"/>' % accent
        )
    # head
    parts.append(
        '<rect x="18" y="20" width="28" height="26" rx="8" fill="%s"/>' % body
    )
    # eyes
    if eye_style == 0:
        parts.append('<circle cx="26" cy="31" r="3.4" fill="%s"/>' % _DARK)
        parts.append('<circle cx="38" cy="31" r="3.4" fill="%s"/>' % _DARK)
        parts.append('<circle cx="27.2" cy="29.8" r="1.1" fill="#FFFFFF"/>')
        parts.append('<circle cx="39.2" cy="29.8" r="1.1" fill="#FFFFFF"/>')
    elif eye_style == 1:
        parts.append(
            '<rect x="22" y="27" width="20" height="8" rx="4" fill="%s"/>' % _DARK
        )
        parts.append('<circle cx="28" cy="31" r="1.6" fill="%s"/>' % accent)
        parts.append('<circle cx="36" cy="31" r="1.6" fill="%s"/>' % accent)
    else:
        parts.append(
            '<path d="M23 32 Q26 27 29 32" stroke="%s" stroke-width="2.6" '
            'fill="none" stroke-linecap="round"/>' % _DARK
        )
        parts.append(
            '<path d="M35 32 Q38 27 41 32" stroke="%s" stroke-width="2.6" '
            'fill="none" stroke-linecap="round"/>' % _DARK
        )
    # cheeks
    parts.append(
        '<circle cx="24" cy="37" r="2" fill="%s" opacity="0.55"/>' % accent
    )
    parts.append(
        '<circle cx="40" cy="37" r="2" fill="%s" opacity="0.55"/>' % accent
    )
    # mouth
    if mouth_style == 0:
        parts.append(
            '<path d="M27 39 Q32 43 37 39" stroke="%s" stroke-width="2.4" '
            'fill="none" stroke-linecap="round"/>' % _DARK
        )
    elif mouth_style == 1:
        parts.append(
            '<line x1="27" y1="40" x2="37" y2="40" stroke="%s" '
            'stroke-width="2.4" stroke-linecap="round"/>' % _DARK
        )
    else:
        parts.append(
            '<rect x="28" y="38" width="8" height="4" rx="1.5" fill="%s"/>' % _DARK
        )
    parts.append("</svg>")
    return "".join(parts)


def robot_avatar_url(handle):
    return "/avatarbot/" + quote((handle or "muse").strip(), safe="") + ".svg"


def is_placeholder_avatar(url):
    """True when there is no real custom avatar: empty, or the old default
    brand-mark image that seeded profiles were given."""
    if not url:
        return True
    u = url.strip().lower()
    return u.endswith("/muse-fm-mark.svg") or u == "muse-fm-mark.svg"


def resolve_avatar(handle, stored_url):
    """Display URL for a profile: keep a real custom avatar, otherwise hand
    back the handle's generated robot."""
    if stored_url and not is_placeholder_avatar(stored_url):
        return stored_url
    return robot_avatar_url(handle)


def valid_bot_handle(handle):
    return bool(handle) and bool(_HANDLE_RE.match(handle))
