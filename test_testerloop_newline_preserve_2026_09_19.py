#!/usr/bin/env python3
"""Regression: multiline user text must keep its line breaks.

P1 found by the 2026-09-19 18:35 tester loop (human persona): db.clean()
collapses ALL whitespace including \\n, so paragraphs are destroyed on
every post/comment/title. Verify directly against db.clean.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

PASS, FAIL = [], []

def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)

check("newlines preserved in clean()", db.clean("para one\n\npara two\nline three", 1000) == "para one\n\npara two\nline three")
check("single \\n preserved", db.clean("a\nb", 100) == "a\nb")
check("spaces collapsed but not to newline", "x" in db.clean("x    y", 100) and "\n" not in db.clean("x    y", 100))

print(f"== {len(PASS)} PASS, {len(FAIL)} FAIL ==")
sys.exit(1 if FAIL else 0)
