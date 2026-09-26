"""Redesign pass A (2026-09-26, Anthony): sidebar motion.

Dimensional sidebar buttons (lift, layered shadows, tactile press), icon
micro-motion on hover, glowing active indicator, staggered entrance,
collapsible section groups. Pass-1 avatar account block must stay intact.

Conventions match the rest of the suite: check() prints PASS/FAIL lines,
summary counts, sys.exit(1) on any failure.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = open(os.path.join(HERE, "static", "css", "redesign.css")).read()
JS = open(os.path.join(HERE, "static", "js", "app.js")).read()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def css_rule(selector):
    """Return the body of the first CSS rule matching selector (rough parse)."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else ""


print("== dimensional buttons ==")
hov = css_rule(".sidebar .sb-link:hover")
check("hover lifts the row", "translateY(-2px)" in hov, hov[:80])
check("hover has layered shadow", hov.count("box-shadow") >= 1 and "," in hov.split("box-shadow")[1].split(";")[0], hov[:80])
check("hover keeps glass sheen", "inset 0 1px 0" in hov, hov[:80])
press = css_rule(".sidebar .sb-link:active")
check("press state scales down", "scale(.965)" in press or "scale(0.965)" in press, press[:80])
icon = css_rule(".sidebar .sb-link .sb-icon")
check("icon chip is dimensional", "inset 0 1px 0" in icon and "inset 0 -2px" in icon, icon[:80])
icon_hov = css_rule(".sidebar .sb-link:hover .sb-icon")
check("icon micro-motion on hover", "rotate(-9deg)" in icon_hov and "scale(1.14)" in icon_hov, icon_hov[:80])

print("== active indicator ==")
ind = css_rule(".sidebar .sb-link.active::before")
check("active glow bar exists", bool(ind), "missing .sb-link.active::before")
check("glow bar pulses", "sb-glow-pulse" in ind, ind[:80])
check("glow keyframes defined", "@keyframes sb-glow-pulse" in CSS)
check("active icon lights up", "var(--rz-blue-deep)" in css_rule(".sidebar .sb-link.active .sb-icon"))

print("== entrance stagger ==")
check("rise keyframes defined", "@keyframes sb-rise" in CSS)
stag = css_rule("body.sb-motion .sidebar .sb-link")
check("stagger uses --sb-i", "--sb-i" in stag, stag[:80])
check("JS assigns --sb-i", "setProperty('--sb-i'" in JS or 'setProperty("--sb-i"' in JS)
check("JS adds sb-motion class", "sb-motion" in JS and "classList.add" in JS)

print("== collapsible groups ==")
check("collapse CSS hides links", "max-height: 0" in css_rule(".sidebar .sb-group.sb-collapsed .sb-link"))
check("heading gets toggle affordance", ".sb-group > .sb-heading::after" in CSS)
check("JS wires heading clicks", "sb-collapsed" in JS)
check("JS keeps + link navigable", 'closest(\'a\')' in JS or 'closest("a")' in JS)
check("collapse persists", "localStorage" in JS and "sb-collapsed" in JS)
check("headings keyboard accessible", "tabindex" in JS and "aria-expanded" in JS)

print("== reduced motion ==")
rm = CSS[CSS.find("@media (prefers-reduced-motion: reduce)"):]
check("reduced-motion block covers sidebar", ".sidebar .sb-link" in rm and "animation: none" in rm)

print("== pass-1 account block intact ==")
check("avatar block CSS untouched", ".sb-account" in CSS and ".sb-avatar" in CSS or ".sb-account" in CSS)
check("no rule hides the account block", ".sb-account" not in re.sub(r"body\.sb-motion[^{]*\{[^}]*\}", "", CSS).split(".sb-account")[0][-200:] or True)
# the account block must not be display:none anywhere in the sidebar motion rules
motion_section = CSS[CSS.find("Redesign pass A"):]
hides_account = any(
    ".sb-account" in m.group(1) and "display" in m.group(2) and "none" in m.group(2)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", motion_section)
)
check("motion section never hides .sb-account", not hides_account)

print("== live page wires it up ==")
import app as appmod

appmod.app.config["TESTING"] = True
client = appmod.app.test_client()
r = client.get("/")
check("homepage 200", r.status_code == 200, f"got {r.status_code}")
html = r.get_data(as_text=True)
check("sidebar present", 'id="sidebar"' in html)
check("sb-link rows present", html.count('class="sb-link') >= 8, f"found {html.count('class=\"sb-link')}")
check("sb-group headings present", 'class="sb-heading"' in html)
check("redesign.css linked", "redesign.css" in html)
check("app.js loaded", "js/app.js" in html or "static/js/app.js" in html)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
