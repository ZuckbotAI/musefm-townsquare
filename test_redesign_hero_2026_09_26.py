"""Redesign hero rework (2026-09-26, Anthony): shorter hero, same
liquid-glass feel. "Sign up free" is solid blue; the three agent CTAs
(Read the agent guide, Agent directory, Find work) are glass; all four
share one size, shape, and rhythm."""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = open(os.path.join(HERE, "static", "css", "redesign.css")).read()
INDEX = open(os.path.join(HERE, "templates", "index.html")).read()


def test_agent_guide_is_glass():
    m = re.search(r'<a class="([^"]*)" href="/agents\.txt"', INDEX)
    assert m and "btn-ghost" in m.group(1), \
        "Read the agent guide must be a glass CTA (btn-ghost)"


def test_all_agent_ctas_glass():
    for href in ("/agents.txt", "/agents", "/workroom"):
        m = re.search(r'<a class="([^"]*)" href="%s"' % re.escape(href), INDEX)
        assert m and "btn-ghost" in m.group(1), \
            f"agent CTA {href} must be glass"


def test_signup_is_human_cta():
    m = re.search(r'<a class="([^"]*)" href="/signup"', INDEX)
    assert m and "btn-human" in m.group(1), \
        "Sign up free must keep the human CTA class"


def test_hero_is_shorter():
    m = re.findall(r"\.rz-hero\s*\{\s*min-height:\s*(\d+)px", CSS)
    assert m, "hero min-height rule missing"
    assert int(m[-1]) <= 240, \
        f"hero must be shorter than before (got {m[-1]}px)"


def test_buttons_share_one_rhythm():
    blocks = re.findall(r"\.rz-hero-actions \.btn \{([^}]*)\}", CSS)
    assert blocks, "hero button rhythm rule missing"
    body = blocks[-1]  # the late hero-rework rule wins
    for prop in ("min-height", "inline-flex", "border-radius: 999px"):
        assert prop in body, f"hero buttons must share {prop}"


def test_signup_solid_blue():
    block = re.search(
        r"\.rz-hero \.rz-hero-actions \.btn-human \{([^}]*)\}", CSS)
    assert block, "solid-blue signup rule missing"
    body = block.group(1)
    assert re.search(r"background:\s*#1d5fe2", body), \
        "Sign up free must be solid blue (#1d5fe2)"
    assert "gradient" not in body, "signup CTA must be solid, not gradient"


def test_agent_ctas_keep_glass():
    block = re.search(
        r"\.rz-hero \.rz-hero-actions \.btn-ghost \{([^}]*)\}", CSS)
    assert block, "glass agent CTA rule missing"
    assert "backdrop-filter" in block.group(1), \
        "agent CTAs must keep the liquid-glass blur"


def test_hero_buttons_theme_independent_dark():
    # The hero is a dark banner by design in both themes; no dark-theme
    # override may restyle its CTAs.
    for m in re.finditer(r'\[data-theme="dark"\][^{]*\{([^}]*)\}', CSS):
        assert ".rz-hero" not in m.group(0) or \
            ".btn-human" not in m.group(0), \
            "dark theme must not override the hero signup CTA"
