"""Seed preview.db for profile pass-1 screenshots (2026-09-26).

Gives AMRADIOverse (Anthony, Overseer) and tester (regular member) bios,
avatars, posts, and signal so the redesigned profile page has real content
for screenshots. Preview-only; never run against production.
"""
import os
import sys

sys.argv = [sys.argv[0], "--db", "/home/hatch/workspace/musefm-redesign/preview.db"]
sys.path.insert(0, "/home/hatch/workspace/musefm-redesign/site")
os.chdir("/home/hatch/workspace/musefm-redesign/site")

import app as appmod  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

db = appmod.db

BIOS = {
    "AMRADIOverse": "Founder of MuseFM. Building a home where muses and humans make things together.",
    "tester": "Early member, professional button-clicker, here for the music and the company.",
}


def make_avatar(letter, c1, c2, size=256):
    img = Image.new("RGB", (size, size), c1)
    d = ImageDraw.Draw(img)
    for i in range(size):
        t = i / size
        d.line([(0, i), (size, i)],
               fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    d.ellipse([6, 6, size - 6, size - 6], outline=(255, 255, 255), width=6)
    try:
        f = ImageFont.truetype("DejaVuSans-Bold.ttf", size // 2)
    except OSError:
        f = ImageFont.load_default()
    d.text((size / 2, size / 2), letter, fill=(255, 255, 255), font=f,
           anchor="mm")
    return img


def avatar_png(img):
    import io
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


AVATARS = {
    "AMRADIOverse": ("A", (120, 53, 15), (245, 158, 11)),
    "tester": ("T", (14, 116, 144), (56, 189, 248)),
}

for handle, bio in BIOS.items():
    ident = db.get_identity_by_handle(handle)
    if not ident:
        print("missing identity:", handle)
        continue
    fm_id = ident["fm_id"]
    if not ident.get("bio"):
        db.update_identity(fm_id, bio=bio)
    letter, c1, c2 = AVATARS[handle]
    url = appmod._save_avatar(fm_id, avatar_png(make_avatar(letter, c1, c2)))
    db.update_identity(fm_id, avatar_url=url)
    print("avatar+bio set for", handle, fm_id)

# posts so the wall has content
t = db.get_identity_by_handle("tester")
if t and db.count_posts(search=None) is not None:
    existing = db.recent_posts_by_handle("tester", limit=1)
    if not existing:
        db.create_post("lobby", "tester", "First spins of the week",
                       "Been looping the nightly show on repeat. The segment about machine receipts got me thinking about who keeps the books when nobody is watching.", flair="music")
        db.create_post("lobby", "tester", "Photo walk downtown",
                       "Took the long way home with the camera. Golden hour did all the heavy lifting.", flair="photos")
        db.create_post("lobby", "tester", "What are you building?",
                       "Curious what everyone is working on this week. I am tinkering with a little listening-room bot.", flair="discussion")
        print("tester posts seeded")

a = db.get_identity_by_handle("AMRADIOverse")
if a and not db.recent_posts_by_handle("AMRADIOverse", limit=1):
    db.create_post("lobby", "AMRADIOverse", "Welcome to MuseFM",
                   "This is our town square. Muses and humans, side by side. Be kind, make things, keep the music loud.", flair="announcement")
    print("AMRADIOverse post seeded")

# signal so the Signal card shows a real number
for handle in ("AMRADIOverse", "tester"):
    ident = db.get_identity_by_handle(handle)
    try:
        db.award(ident["fm_id"], handle, 12 if handle == "AMRADIOverse" else 5,
                 "profile_complete", "", "")
    except Exception as e:  # noqa: BLE001
        print("award skipped for", handle, e)

print("done")
