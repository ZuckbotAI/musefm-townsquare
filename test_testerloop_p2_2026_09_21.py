"""Tester-loop 2026-09-21 (00:35 run): two new P2s from adversarial QA, both
code-confirmed in the bug report.

1. Re-flagging returns a bogus flag_id: db.flag_post() used
   INSERT ... ON CONFLICT DO UPDATE and returned cur.lastrowid — on the
   update path SQLite's lastrowid is a stale rowid from an unrelated insert
   on the reused connection, not the row's real id.

2. NUL byte accepted in comment body: db.clean() normalized whitespace but
   never stripped NUL/C0 control chars; stored verbatim.

Both are now fixed in db.py. This file proves it.

Run: python3 test_testerloop_p2_2026_09_21.py
"""
import base64
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import Database, clean, ensure_forum_flags_schema

def fake_key(seed):
    raw = bytes((seed * 7 + i * 13) % 256 for i in range(32))
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


# --- P2 1: flag_post returns the real row id on re-flag (upsert update path)
tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
try:
    d = Database(tmp.name)
    ensure_forum_flags_schema(d)
    # minimal identities + a post to flag
    d.register_identity("flagger1", fake_key(1))
    d.register_identity("poster9", fake_key(9))
    # 'lobby' is seeded by the schema
    pid = d.create_post("lobby", "poster9", "flag target", "flag target body")
    fm_id = d._one("SELECT fm_id FROM identities WHERE handle='flagger1'")["fm_id"]

    f1 = d.flag_post("post", pid, fm_id, "flagger1", "spam")
    n1 = d._one("SELECT COUNT(*) c FROM post_flags")["c"]
    check("first flag returns a real id",
          isinstance(f1, int) and f1 > 0, repr(f1))
    check("one row after first flag", n1 == 1, repr(n1))

    f2 = d.flag_post("post", pid, fm_id, "flagger1", "misinfo")
    rows = d._q("SELECT id, reason FROM post_flags")
    check("re-flag returns the SAME real id (not a phantom)",
          f2 == f1, "first=%r second=%r rows=%r" % (f1, f2, rows))
    check("re-flag still one row", len(rows) == 1, repr(rows))
    check("re-flag updates the reason", rows[0]["reason"] == "misinfo",
          repr(rows[0]["reason"]))
    f3 = d.flag_post("post", pid, fm_id, "flagger1", "other")
    check("third flag also returns the real id", f3 == f1, repr(f3))
finally:
    os.unlink(tmp.name)

# --- P2 2: clean() strips NUL and C0 control chars
check("NUL stripped from prose",
      clean("a\x00b null test", 2000) == "ab null test",
      repr(clean("a\x00b null test", 2000)))
check("C0 controls stripped, newline kept",
      clean("x\x01\x02\x7fy\nz", 2000) == "xy\nz",
      repr(clean("x\x01\x02\x7fy\nz", 2000)))
check("single_line: NUL stripped, tabs collapsed",
      clean("t\x00\tx", 2000, single_line=True) == "t x",
      repr(clean("t\x00\tx", 2000, single_line=True)))
check("normal prose untouched",
      clean("hello  world\n\n\nfine", 2000) == "hello world\n\nfine",
      repr(clean("hello  world\n\n\nfine", 2000)))
check("CRLF normalize still works",
      clean("a\r\nb", 2000) == "a\nb", repr(clean("a\r\nb", 2000)))
check("empty/None safe", clean(None, 10) == "" and clean("", 10) == "")

print("== %d PASS, %d FAIL ==" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
