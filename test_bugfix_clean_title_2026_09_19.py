#!/usr/bin/env python3
"""Bug-fix proof 2026-09-19: UUID-like titles fully cleaned, no fragments.

P2 (tester loop 15:35 run): clean_title() removed only 8+ hex segments,
leaking 4-char UUID fragments into display titles:
  "Users 2babe7f6 44b8 B6bd A4e4865dbb89 Ge" -> "Users 44b8 B6bd Ge"
  "media-generation-burst-d1-compile-0-2babe7f6-44b8-b6bd-a4e4865dbb89.mp4"
    -> "Compile 44b8 B6bd"
The truncated-UUID blob (8-4-4-12) also slipped past _UUID_RE (which only
matches valid 8-4-4-4-12 UUIDs).

Fix: strip the ENTIRE hex run — space-separated and dash-joined —
including 4-char segments. Ordinary titles ("dead beef cafe",
"dead-beef-cafe", "song-2024-remix") stay untouched.

All display surfaces (Shorts, watch page, upload listings/API,
provenance display) funnel through videos.clean_title().

Run: python3 test_bugfix_clean_title_2026_09_19.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from videos import clean_title

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def main():
    # the exact bad title from the tester report: no hex may survive
    out = clean_title("Users 2babe7f6 44b8 B6bd A4e4865dbb89 Ge")
    check("space-separated UUID title: no hex fragments",
          not any(frag in out for frag in
                  ("2babe7f6", "44b8", "B6bd", "b6bd", "A4e4865dbb89")),
          repr(out))
    print("    -> %r" % out)

    # the dashed media-generation filename: exact expected output
    out = clean_title(
        "media-generation-burst-d1-compile-0-2babe7f6-44b8-b6bd-a4e4865dbb89.mp4")
    check("dashed filename cleans to exactly 'Compile'", out == "Compile",
          repr(out))

    # uppercase dashed blob embedded in a normal title
    out = clean_title("clip 0-2BABE7F6-44B8-B6BD-A4E4865DBB89 end")
    check("uppercase dashed blob fully removed",
          "2BABE7F6" not in out and "44B8" not in out, repr(out))

    # ordinary titles must NOT be mangled
    for t in ("dead beef cafe", "dead-beef-cafe", "My vacation video",
              "song-2024-remix"):
        check("ordinary title untouched: %r" % t, clean_title(t) == t,
              repr(clean_title(t)))

    check("empty -> 'untitled clip'", clean_title("") == "untitled clip")

    print("== %d PASS, %d FAIL ==" % (len(PASS), len(FAIL)))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
