"""Link card image (2026-09-26, Anthony): social link preview art.

Anthony supplied a new link card image; it ships as
static/img/link-card.jpg and every shared musefm.lol link pulls it via
og:image + twitter:card (summary_large_image) + twitter:image meta tags
in the base template.

Conventions match the rest of the suite: check() prints PASS/FAIL lines,
summary counts, exit 1 on any failure.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = open(os.path.join(HERE, "templates", "base.html")).read()
CARD = os.path.join(HERE, "static", "img", "link-card.jpg")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


CARD_URL = "https://musefm.lol/static/img/link-card.jpg"

print("== card asset on disk ==")
check("link-card.jpg exists", os.path.isfile(CARD))
with open(CARD, "rb") as f:
    magic = f.read(4)
check("link-card.jpg is a JPEG", magic[:3] == b"\xff\xd8\xff")
from PIL import Image
im = Image.open(CARD)
check("JPEG loads", im.format == "JPEG", f"format={im.format}")
check("card is 1206x658", im.size == (1206, 658), f"size={im.size}")
check("card is landscape (wider than tall)", im.size[0] > im.size[1])
check("card file is not tiny", os.path.getsize(CARD) > 50_000,
      f"bytes={os.path.getsize(CARD)}")

print("== base template meta tags ==")
check("og:image points at link-card.jpg",
      f'<meta property="og:image" content="{CARD_URL}">' in BASE)
check("og:image:width matches asset (1206)",
      '<meta property="og:image:width" content="1206">' in BASE)
check("og:image:height matches asset (658)",
      '<meta property="og:image:height" content="658">' in BASE)
check("og:image:alt describes the card",
      '<meta property="og:image:alt" content="musefm.lol' in BASE)
check("twitter:card is summary_large_image",
      '<meta name="twitter:card" content="summary_large_image">' in BASE)
check("twitter:image points at link-card.jpg",
      f'<meta name="twitter:image" content="{CARD_URL}">' in BASE)
check("old og-image.png is no longer referenced", "og-image.png" not in BASE)
check("og:image is an absolute https URL", CARD_URL.startswith("https://"))
card_meta_lines = [ln for ln in BASE.splitlines()
                   if "og:image" in ln or "twitter:image" in ln]
check("no em dash in the card meta copy",
      all("\u2014" not in ln for ln in card_meta_lines),
      f"lines checked: {len(card_meta_lines)}")

print("== meta tags render on key pages ==")
import app as appmod

appmod.app.config["TESTING"] = True
client = appmod.app.test_client()

PAGES = ["/", "/c/lobby", "/wall", "/episodes", "/shorts"]
for page in PAGES:
    r = client.get(page)
    ok = r.status_code == 200
    check(f"{page} renders 200", ok, f"got {r.status_code}")
    if not ok:
        continue
    html = r.get_data(as_text=True)
    check(f"{page} renders og:image with link-card.jpg", CARD_URL in html)
    check(f"{page} renders twitter:card summary_large_image",
          '<meta name="twitter:card" content="summary_large_image">' in html)
    check(f"{page} renders twitter:image with link-card.jpg",
          f'<meta name="twitter:image" content="{CARD_URL}">' in html)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
