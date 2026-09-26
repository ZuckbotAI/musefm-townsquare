"""Redesign pass D3 (2026-09-26, Anthony): tighten the search liquid-glass.

Direct checks:
- the stale pre-glass .topbar-search rule block is neutralized (per the
  stale-CSS quirk: old same-class rules silently break redesigns)
- the surviving glass block has crisper blur/saturation, edge highlights,
  a premium hover state, and a precisely aligned Ctrl+K chip
- dark-theme glass overrides exist for the search, chip, and focus ring
- the search form still renders and submits (homepage HTTP 200)
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = open(os.path.join(HERE, "static", "css", "redesign.css")).read()


def glass_block():
    # The surviving (later) main .topbar-search glass rule: the one that
    # carries the backdrop-filter. Media-query tweaks don't count.
    blocks = [m.group(1) for m in
              re.finditer(r'\.topbar-search\s*\{([^}]*)\}', CSS)
              if "backdrop-filter" in m.group(1)]
    assert blocks, "no .topbar-search glass rule found"
    return blocks[-1]


def test_stale_search_block_neutralized():
    blocks = [m.group(1) for m in
              re.finditer(r'\.topbar-search\s*\{([^}]*)\}', CSS)]
    for b in blocks[:-1]:
        assert "position: absolute" not in b and "translateX(-50%)" not in b, \
            "stale absolute-positioned search block still active"


def test_glass_is_crisp():
    b = glass_block()
    assert "backdrop-filter" in b and "blur(" in b
    m = re.search(r'blur\((\d+)px\)', b)
    assert m and int(m.group(1)) >= 18, "blur should be crisp (>=18px)"
    assert "saturate(" in b, "glass needs saturation boost"
    assert "inset 0 1px 0" in b, "glass needs a top edge highlight"


def test_search_hover_state():
    assert re.search(r'\.topbar-search:hover\s*\{', CSS), \
        "search needs a premium hover state"


def test_kbd_chip_aligned():
    bodies = [m.group(1) for m in
              re.finditer(r'(?<!"dark"] )\.topbar-search kbd\s*\{([^}]*)\}', CSS)
              if "border" in m.group(1)]
    assert bodies, "kbd chip rule missing"
    body = bodies[-1]
    assert "align-self: center" in body or "align-items" in body, \
        "kbd chip must be vertically centered"
    assert "white-space: nowrap" in body


def test_search_dark_mode():
    assert '[data-theme="dark"] .topbar-search' in CSS, \
        "dark theme must restyle the search glass"
    assert '[data-theme="dark"] .topbar-search kbd' in CSS or \
        re.search(r'\[data-theme="dark"\][^{]*kbd', CSS), \
        "dark theme must restyle the Ctrl+K chip"


def test_search_form_renders():
    import app as appmod
    client = appmod.app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'class="topbar-search"' in body
    assert "<kbd>Ctrl K</kbd>" in body
