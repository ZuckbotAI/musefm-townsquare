"""Redesign pass C (2026-09-26, Anthony): forum post cards.

Audit fix: the sort tabs, search, and New post merge into one control
deck; post cards get a tightened spacing rhythm; the actions row becomes
one integrated segmented strip with the signal rollout docked in.

Conventions match the rest of the suite: check() prints PASS/FAIL lines,
summary counts, exit 1 on any failure.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
CSS = open(os.path.join(HERE, "static", "css", "redesign.css")).read()
TPL = open(os.path.join(HERE, "templates", "community.html")).read()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def css_rule(selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    return m.group(1) if m else ""


def css_rule_last(selector):
    """Last matching rule — later sections win in the cascade."""
    ms = list(re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", CSS))
    return ms[-1].group(1) if ms else ""


print("== one control deck ==")
check("feed-deck class in template", "feed-deck" in TPL)
check("no separate search-row form", 'class="search-row"' not in TPL)
check("search keeps sort + query params",
      'name="sort"' in TPL and 'name="q"' in TPL and 'value="{{ q }}"' in TPL)
check("deck is one glass bar", "border-radius: 18px" in css_rule(".feed-deck"))
check("deck holds tabs, search, new post",
      ".feed-deck .seg-tabs" in CSS and ".feed-search" in CSS and ".feed-deck .feed-new" in CSS)
check("search input has focus ring", "box-shadow" in css_rule(".feed-search .input:focus"))

print("== tightened card rhythm ==")
check("card padding tightened", "13px 16px 12px" in css_rule_last(".rz-forum-post"))
check("meta margin trimmed", "margin-bottom: 3px" in css_rule_last(".rz-forum-post .post-meta"))
check("title margin trimmed", "margin: 1px 0 4px" in css_rule_last(".rz-forum-post .post-title"))
check("excerpt styled tight", "margin: 3px 0 0" in css_rule_last(".rz-forum-post .post-excerpt"))
check("avatar 40px", "width: 40px" in css_rule_last(".rz-forum-post .post-avatar"))

print("== integrated action strip ==")
strip = css_rule_last(".rz-forum-post .post-actions")
check("strip is one segmented pill", "border-radius: 999px" in strip and "inline-flex" in strip)
check("segments divided", "border-left" in css_rule_last(".rz-forum-post .post-actions > * + *"))
check("no floating hairline above", "border-top" not in strip)
roll = css_rule_last(".rz-forum-post .sig-rollout")
check("rollout docks as segment", "background: transparent" in roll and "border: 0" in roll)
check("open tray floats anchored", "position: absolute" in css_rule_last(".rz-forum-post .sig-tray"))
check("votes are a segment, not pushed right",
      "margin-left: 0" in css_rule_last(".rz-forum-post .votes"))

print("== no em dashes in touched copy ==")
check("template copy clean", "—" not in TPL)

print("== live forum page ==")
import app as appmod

appmod.app.config["TESTING"] = True
client = appmod.app.test_client()
r = client.get("/c/lobby")
check("/c/lobby 200", r.status_code == 200, f"got {r.status_code}")
html = r.get_data(as_text=True)
check("deck rendered", "feed-deck" in html)
check("post cards rendered", "rz-forum-post" in html)
check("reaction widgets present", "rxn-rollout" in html)
check("search form posts to community", 'action="/c/lobby"' in html)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
