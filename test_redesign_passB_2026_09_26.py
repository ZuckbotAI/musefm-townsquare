"""Redesign pass B (2026-09-26, Anthony): In the Air composer polish and the
living orb.

The orb breathes at rest, ripples on keystroke, and its glow ramps with
input length (--zb-charge). The composer card gets tighter styling, a
focus ring, and a live character counter. The stray </div> that broke the
/wall composer form is fixed.

Conventions match the rest of the suite: check() prints PASS/FAIL lines,
summary counts, exit 1 on any failure.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = open(os.path.join(HERE, "static", "css", "redesign.css")).read()
JS = open(os.path.join(HERE, "static", "js", "zuckbot-orb.js")).read()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def css_rule(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else ""


print("== orb aliveness ==")
check("breathe keyframes defined", "@keyframes zb-breathe" in CSS)
passb = CSS[CSS.find("Redesign pass B"):]
inner_b = re.search(r"\.zuckbot-orb-inner\s*\{([^}]*)\}", passb)
check("inner breathes at rest", bool(inner_b) and "zb-breathe" in inner_b.group(1))
aura = css_rule(".zuckbot-orb::before")
check("aura halo exists", bool(aura), "missing .zuckbot-orb::before")
check("aura ramps with charge", "--zb-charge" in aura, aura[:100])
check("ripple keyframes defined", "@keyframes zb-ripple" in CSS)
check("ripple rule on .zb-hit", "zb-ripple" in css_rule(".zuckbot-orb.zb-hit::after"))
core = css_rule(".zuckbot-orb .zb-core")
check("core swells with charge", "--zb-charge" in core, core[:100])
check("listening keeps bob+glow", "zb-bob" in css_rule(".zuckbot-orb.is-listening .zuckbot-orb-inner"))
check("aura has no layout shift", "pointer-events: none" in aura)

print("== orb JS behavior ==")
check("JS sets --zb-charge", "--zb-charge" in JS)
check("charge derived from input length", "ta.value.length" in JS and "ta.maxLength" in JS)
check("ripple throttled", "RIPPLE_MS" in JS and "lastRipple" in JS)
check("ripple retriggers animation", "offsetWidth" in JS)
check("counter injected", "zb-count" in JS)
check("counter warns near limit", "zb-low" in JS and "zb-empty" in JS)
check("reduced motion bails out", "prefers-reduced-motion" in JS)
check("listening still settles", "is-listening" in JS and "SETTLE_MS" in JS)

print("== composer polish ==")
comp = css_rule(".wall-composer")
check("composer has focus-within ring", "box-shadow" in css_rule(".wall-composer:focus-within"))
check("counter styled", bool(css_rule(".zb-count")))
check("post button tactile", "scale(.95)" in css_rule(".wall-composer .btn:active"))

print("== wall form markup fixed ==")
wall = open(os.path.join(HERE, "templates", "wall.html")).read()
check("stray closing div gone", "</div>\n    </div>\n    <div class=\"wall-composer-row\">" not in wall)
check("submit button inside the form",
      wall.index("<form") < wall.index("Post note") < wall.index("</form>"))

print("== reduced motion ==")
tail = CSS[CSS.find("Redesign pass B"):]
rm = tail[tail.find("@media (prefers-reduced-motion: reduce)"):]
check("reduced motion kills orb motion", "zb-breathe" not in rm and "animation: none" in rm)

print("== live pages ==")
import app as appmod

appmod.app.config["TESTING"] = True
client = appmod.app.test_client()
for path in ("/", "/wall"):
    r = client.get(path)
    check(f"{path} 200", r.status_code == 200, f"got {r.status_code}")
    html = r.get_data(as_text=True)
    check(f"{path} has orb", "zuckbot-orb" in html)
    check(f"{path} loads orb script", "zuckbot-orb.js" in html)
    # homepage composer renders only for signed-in users; /wall covers the composer
    if path == "/wall":
        check(f"{path} has composer", "wall-composer" in html)
    else:
        check(f"{path} has In the Air section", "in-the-air" in html)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
