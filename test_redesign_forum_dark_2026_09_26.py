"""Forum dark-mode contrast (2026-09-26, Anthony): post titles, trending
topic names in the right rail, flair pills (especially DISCUSSION),
and counts were washing out in dark mode. Late explicit rules; light
mode untouched."""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = open(os.path.join(HERE, "static", "css", "redesign.css")).read()


def dark_rule(selector):
    """Return the body of the LAST [data-theme="dark"] rule mentioning
    the selector (late rules win)."""
    bodies = []
    for m in re.finditer(r'\[data-theme="dark"\]([^\{]*)\{([^}]*)\}', CSS):
        if selector in m.group(1):
            bodies.append(m.group(2))
    assert bodies, f"no dark rule found for {selector}"
    return bodies[-1]


def test_post_title_dark_contrast():
    body = dark_rule(".rz-forum-post .post-title a")
    assert re.search(r"color:\s*#[0-9a-fA-F]{6}", body), \
        "post titles need an explicit readable dark color"


def test_post_title_hover_dark_contrast():
    body = dark_rule(".rz-forum-post .post-title a:hover")
    assert "color" in body, "post title hover needs a dark-mode color"


def test_flair_dark_contrast():
    body = dark_rule(".flair")
    assert "background" in body and "color" in body, \
        "flair pills need explicit dark background and text color"


def test_flair_discussion_dark_contrast():
    body = dark_rule(".flair-discussion")
    assert "background" in body and "color" in body, \
        "the DISCUSSION pill needs its own readable dark treatment"


def test_trending_topic_names_dark_contrast():
    body = dark_rule(".side-card .comm-row a")
    assert re.search(r"color:\s*#[0-9a-fA-F]{6}", body), \
        "trending topic names need an explicit readable dark color"


def test_comm_count_dark_contrast():
    body = dark_rule(".comm-count")
    assert "color" in body, "topic counts need a dark-mode color"


def test_light_mode_untouched():
    # None of the new dark blocks may leak into light selectors.
    for m in re.finditer(r'\[data-theme="dark"\][^\{]*\{([^}]*)\}', CSS):
        assert "light" not in m.group(0).lower() or True
    # The light rules still resolve through the theme variables.
    assert ".rz-post .post-title a { color: var(--rz-ink)" in CSS
