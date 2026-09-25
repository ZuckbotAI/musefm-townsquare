#!/usr/bin/env python3
"""
Tests for the Pets homepage promo (2026-09-19):
- the homepage renders a Pets promo band (section.pet-promo)
- the band shows a showcase pet (inline SVG art, no external image asset)
- the CTA links to /pet and carries a small pet icon inside the button
- the promo band sits between the Shorts spotlight and the daily question

Run:  python3 test_tidepal_homepage.py
Throwaway SQLite db + Flask test client. Nothing touches townsquare.db.
"""
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

TEST_DB = "/tmp/test-townsquare-tidepal-home.db"
TEST_DATA = "/tmp/test-townsquare-tidepal-home-data"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def setup():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    if os.path.isdir(TEST_DATA):
        shutil.rmtree(TEST_DATA)
    from db import Database
    appmod.db = Database(TEST_DB)
    appmod.DATA_DIR = TEST_DATA
    os.makedirs(TEST_DATA, exist_ok=True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def t_homepage_promo(client):
    print("== pets homepage promo ==")
    r = client.get("/")
    check("home 200", r.status_code == 200, f"got {r.status_code}")
    html = r.get_data(as_text=True)

    m = re.search(
        r'<section class="card pet-promo"[^>]*>(.*?)</section>',
        html, re.S)
    check("pet-promo section present", m is not None)
    if not m:
        return
    band = m.group(1)

    check("band has showcase pet inline SVG",
          "<svg" in band and 'role="img"' in band)
    check("band pet is real pet art (viewBox 0 0 120 120)",
          'viewBox="0 0 120 120"' in band)
    check("band links CTA to /pet", 'href="/pet"' in band)
    check("CTA button carries a pet icon (btn-pet span with svg)",
          re.search(r'<span class="btn-pet"[^>]*>.*?<svg', band, re.S) is not None)
    check("band has Pets flair tag", "Pets" in band)
    check("band headline present", "Meet your Pet" in band)

    # ordering: promo band after Shorts spotlight, before daily question
    shorts_pos = html.find("shorts-spotlight")
    promo_pos = html.find("pet-promo")
    daily_pos = html.find("daily-ritual")
    if shorts_pos != -1:
        check("promo band renders after Shorts spotlight",
              promo_pos > shorts_pos,
              f"shorts={shorts_pos} promo={promo_pos}")
    else:
        # Fresh test DB has no shorts, so the spotlight block is skipped;
        # verify placement from the template source instead. Scope to the
        # Pets band itself: the For-agents promo moved to the top of
        # the page as a compact card (2026-09-23, Anthony) and now holds the
        # first "pet-promo" occurrence in the template.
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "templates", "index.html")).read()
        check("pets band placed after Shorts block in template",
              src.find('aria-label="Pets"') > src.find("shorts-spotlight"))
    # For-agents promo: compact card pinned to the very top of the page
    # (2026-09-23, Anthony).
    agents_pos = html.find('aria-label="For agents"')
    check("for-agents promo present and compact", agents_pos != -1 and
          'pet-promo compact' in html[:agents_pos + 200])
    check("for-agents promo renders before the hero",
          agents_pos != -1 and agents_pos < html.find("fm-hero"),
          f"agents={agents_pos} hero={html.find('fm-hero')}")
    check("promo band renders before daily question",
          daily_pos == -1 or promo_pos < daily_pos,
          f"promo={promo_pos} daily={daily_pos}")


def t_no_external_pet_asset(client):
    print("== no external pet asset needed ==")
    html = client.get("/").get_data(as_text=True)
    m = re.search(
        r'<section class="card pet-promo"[^>]*>(.*?)</section>',
        html, re.S)
    band = m.group(1) if m else ""
    check("band uses no <img> pet asset (pure inline SVG)",
          "<img" not in band)


if __name__ == "__main__":
    client = setup()
    t_homepage_promo(client)
    t_no_external_pet_asset(client)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
