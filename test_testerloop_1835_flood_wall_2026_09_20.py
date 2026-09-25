"""Tester-loop 2026-09-20 18:35 run — P2: 2000-emoji comment renders as a wall.

Repro: POST /post/8/comment with body "🔥"×2000 -> 302; the full wall
rendered inline in /c/lobby/post/8.

Fix (app.py link_mentions wrapper + .flood-wall CSS): any 40+ run of the
same glyph collapses to a 60-char preview + native <details> expander
holding the FULL text (same escape/linkify/mention pipeline — XSS
guarantees unchanged). Normal prose never contains a 40-run of one glyph,
so no false positives.

Run: python3 test_testerloop_1835_flood_wall_2026_09_20.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


# 1. Exact repro: 2000-fire wall collapses
wall = "🔥" * 2000
out = appmod.link_mentions(wall)
check("flood wall -> <details class=flood-wall>",
      '<details class="flood-wall">' in out)
check("flood wall -> preview present", "🔥" in out.split("<details")[0])
check("flood wall -> char count in summary",
      "Show full (2000 characters)" in out)
check("flood wall -> full text retained behind expander",
      out.count("🔥") >= 2000, f"counted {out.count('🔥')}")

# 2. No escaping regression on flood path: script content stays inert
evil = "<script>alert(1)</script>" + "🔥" * 100
out = appmod.link_mentions(evil)
check("flood path still escapes HTML",
      "<script>" not in out and "&lt;script&gt;" in out)

# 3. Normal prose untouched: no <details> injected
for label, text in [
    ("plain sentence", "Hello world, this is a normal comment."),
    ("repeated words", "ha " * 30),
    ("39-run under threshold", "a" * 39),
    ("mentions+url", "@wynjr see https://musefm.lol/pet for details"),
    ("multiline", "line one\nline two\nline three"),
]:
    out = appmod.link_mentions(text)
    check(f"normal text unchanged [{label}]", "<details" not in out,
          repr(out[:120]))

# 4. Threshold boundary: 39 same-glyph run -> no collapse; 40 -> collapse
check("39-run no collapse", "<details" not in appmod.link_mentions("x" * 39))
check("40-run collapses", "<details" in appmod.link_mentions("x" * 40))

# 5. Mixed content: wall + legit text + mention still linkifies inside
mixed = "🔥" * 50 + " great point @wynjr"
out = appmod.link_mentions(mixed)
check("mixed -> details present", "<details" in out)
check("mixed -> full retained", out.count("🔥") >= 50)

# 6. mentions filter still registered as the wrapper
check("filter points at wrapper",
      appmod.app.jinja_env.filters["mentions"] is appmod.link_mentions)

# 7. XSS battery still clean on the normal path (spot check)
out = appmod.link_mentions('<img src=x onerror=alert(1)> nice')
check("normal path escapes tags",
      "<img" not in out and "&lt;img" in out)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
