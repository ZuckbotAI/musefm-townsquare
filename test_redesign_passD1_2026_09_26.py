"""Redesign pass D1 (2026-09-26, Anthony): MuseFM Family bar as readable glass.

The dark fmf-bar becomes frosted glass: translucent background + blur in
both light and dark themes, legible text colors per theme, all three
family links intact.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_HTML = open(os.path.join(HERE, "templates", "base.html")).read()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def style_block():
    m = re.search(r"<style>(.*?)</style>", BASE_HTML, re.S)
    return m.group(1) if m else ""


STY = style_block()

print("== glass treatment ==")
check("fmf-bar style block present", ".fmf-bar{" in STY.replace(" ", "") or ".fmf-bar" in STY)
check("bar uses backdrop blur", "backdrop-filter" in STY and "blur(" in STY)
check("bar background is translucent (not opaque #0b1220)",
      "background:#0b1220" not in STY.replace(" ", "") and "rgba(" in STY)
check("subtle border-bottom kept", "border-bottom" in STY)

print("== light theme legibility ==")
check("light label text is dark", "#0f172a" in STY)
check("light link text is dark slate", "#334155" in STY)

print("== dark theme legibility ==")
check("dark theme override exists", '[data-theme="dark"] .fmf-bar' in STY)
check("dark bar is translucent dark", "rgba(11,18,32" in STY)
check("dark link color override exists", '[data-theme="dark"] .fmf-link' in STY)
check("dark label stays light", "#e2e8f0" in STY)

print("== links intact ==")
check("MuseFM link present", ">MuseFM</a>" in BASE_HTML)
check("Playbook link present", "MuseFM Playbook</a>" in BASE_HTML)
check("Trustline link present", "MuseFM Trustline</a>" in BASE_HTML)
check("you-are-here highlight kept", "fmf-here" in BASE_HTML)
check("nav aria-label kept", 'aria-label="MuseFM family sites"' in BASE_HTML)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
