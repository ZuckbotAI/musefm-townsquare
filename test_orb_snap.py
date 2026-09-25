#!/usr/bin/env python3
"""
Tests for the orb's scroll lifecycle (2026-09-23 spec, Anthony).

The orb lives in orb-dock.js with three stages, in order:
  - HERO: on page load, the orb sits in the hero beside the homepage
    headline, ~96px — its home (click for sayings)
  - DOCK: scroll past the hero -> orb moves to a docked "next slot": a
    fixed corner/side dock, smaller (~64px desktop / ~56px mobile)
  - FOLLOW: keep scrolling past a second threshold -> the orb breaks away
    from the dock and follows the user, smaller still (~48px desktop /
    ~44px mobile), trailing the scroll with a soft lag and gentle drift

Hard constraints verified here:
  - no placeholder outlines anywhere (no dashed dock ring, no dashed stage ring)
  - no standalone /zuckbot-says page (301 -> /), sayings only via orb clicks
  - drag-to-place re-enabled (2026-09-23, Anthony): drag becomes a user
    offset on the dock's current target; offset drops on dock state change;
    double-click returns the orb to its hero home
  - no emotional anthropomorphism of the orb in templates/static copy

Run:  python3 test_orb_snap.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + ((" — " + str(detail)) if detail and not cond else ""))


def read(rel):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)) as f:
        return f.read()


def main():
    base = read("templates/base.html")
    dock_js = read("static/js/orb-dock.js")
    orb_js = read("static/js/muse-orb.js")
    css = read("static/css/style.css")
    index = read("templates/index.html")

    # --- home anchor in the topbar ---
    check("base has #orb-home", 'id="orb-home"' in base)
    check("home carries the orb anchor", 'data-muse-orb-anchor' in base)
    home_pos = base.find('id="orb-home"')
    brand_pos = base.find('class="brand"')
    check("home anchor sits next to the brand button",
          brand_pos != -1 and home_pos != -1 and 0 < home_pos - brand_pos < 800)
    check("old fixed dock div is gone", 'class="orb-dock"' not in base and 'id="orb-dock"' not in base)

    # --- drag re-enabled (2026-09-23, Anthony), dock-aware ---
    check("draggable:true option set", "MuseOrbOptions.draggable = true" in base)
    check("orb core honors draggable option", "options.draggable !== false" in orb_js)
    check("drag moves via transform when dock-managed", "dockBase()" in orb_js)
    check("drag kills fly transition while dragging",
          "wrap.style.transition = 'none'" in orb_js)
    check("drop hands the offset to the dock", "dock.setOffset(dropDx, dropDy)" in orb_js)
    check("no stale-position restore fighting the dock",
          "localStorage.getItem(STORAGE_KEY)" not in orb_js)
    check("dock exposes the drag handoff", "window.MuseOrbDock" in dock_js)
    check("dock skips placement mid-drag", "muse-orb-dragging" in dock_js)
    check("dock drops the offset on state change", "lastWhere" in dock_js)
    check("double-click clears the drag offset",
          "off.dx = 0; off.dy = 0;" in dock_js)
    check("no 'drag me' copy", "Drag me" not in orb_js)

    # --- three stages in orb-dock.js: hero -> dock -> follow ---
    for state in ("hero", "dock", "follow"):
        check("dock manages '%s' state" % state, ("'%s'" % state) in dock_js and "where" in dock_js)
    check("hero snaps into #hero-orb-stage", "getElementById('hero-orb-stage')" in dock_js)
    check("hero sits at ~96px", "Math.min(1.0," in dock_js)
    check("dock is viewport-fixed", "window.innerWidth" in dock_js and "dockScale" in dock_js)
    check("dock is ~64px (smaller than hero)", "64 / ORB" in dock_js)
    check("follow breaks away past a second threshold",
          "followAfter" in dock_js and "followScale" in dock_js)
    check("follow is ~48px (smaller than dock)", "48 / ORB" in dock_js)
    check("follow trails the scroll with a lag", "followLag" in dock_js)
    check("follow has a gentle drift animation", "muse-orb-follow" in dock_js and "muse-orb-drift" in css)
    check("double-click returns to the hero home",
          "behavior: 'smooth'" in dock_js and "scrollTo" in dock_js)
    check("no drag-yield logic remains", "pointermove" not in dock_js)
    check("reduced-motion = instant snaps", "prefers-reduced-motion" in dock_js)

    # --- no placeholder outlines anywhere ---
    check("no dashed dock ring in CSS", ".orb-dock::before" not in css)
    check("no dashed stage ring in CSS", ".hero-orb-stage::before" not in css)
    dashed = re.findall(r"\.(?:orb[\w-]*|hero-orb[\w-]*)[^{]*\{[^}]*dashed", css)
    check("no dashed borders on orb elements", not dashed, str(dashed[:3]))
    check("home placeholder is invisible", "#orb-home" in css and "visibility: hidden" in css)
    check("no dock tag label", "orb-dock-tag" not in base and "orb-dock-tag" not in css)

    # --- hero stage still the click-for-sayings home ---
    check("index keeps hero stage", 'id="hero-orb-stage"' in index)
    check("sayings API still wired", "/api/zuckbot-says/random" in orb_js)
    check("no standalone zuckbot-says template refs",
          "zuckbot-says" not in index and "zuckbot_says" not in index)

    # --- /zuckbot-says stays a permanent redirect; no page/section ---
    app_src = read("app.py")
    m = re.search(r"@app\.route\(\"/zuckbot-says\"\)\s*\ndef \w+\(\):\s*\"\"\"([^\"]*)\"\"\"\s*return redirect\(\"/\", code=(\d+)\)",
                  app_src, re.S)
    check("/zuckbot-says is a 301 to /", m is not None and m.group(2) == "301",
          m.group(0)[:80] if m else "route not found")
    check("sayings API route intact", '@app.route("/api/zuckbot-says/random")' in app_src)

    # --- no emotional anthropomorphism of the orb ---
    banned = ["it likes that", "it wants", "it loves", "it feels", "it notices",
              "Drag me", "sends me home"]
    blob = (base + index + orb_js + dock_js).lower()
    hits = [b for b in banned if b.lower() in blob]
    check("no banned orb anthropomorphism", not hits, str(hits))

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
