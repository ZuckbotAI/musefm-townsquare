#!/usr/bin/env python3
"""Maker's Row bond outreach sweep runner.

Evaluates every bonded pet for state-transition triggers (hunger, waiting,
illness, comeback) and sends inbox notifications (+ best-effort webhooks).
Scheduler-ready: run every ~30 minutes against the live townsquare DB.

Usage:
    python3 bond_outreach_sweep.py [/path/to/townsquare.db]
    TOWNSQUARE_DB=/path/to/townsquare.db python3 bond_outreach_sweep.py

Local-only. Nothing here publishes, deploys, or uploads anything.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from db import Database, now  # noqa: E402
import bond as bondmod  # noqa: E402
import pets  # noqa: E402

NOTIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fm_id TEXT NOT NULL,
  type TEXT NOT NULL,
  ref_type TEXT NOT NULL DEFAULT '',
  ref_id TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  read INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_fm ON notifications(fm_id, created_at DESC);
"""


def main():
    path = (sys.argv[1] if len(sys.argv) > 1
            else os.environ.get("TOWNSQUARE_DB", ""))
    if not path or not os.path.exists(path):
        print(json.dumps({"ok": False,
                          "error": "townsquare db not found; pass a path or"
                                   " set TOWNSQUARE_DB"}))
        return 2
    db = Database(path)
    bondmod.ensure_bond_schema(db)
    pets.ensure_pet_schema(db)
    pets.ensure_care_schema(db)
    for stmt in NOTIFICATIONS_DDL.strip().split(";\n"):
        if stmt.strip():
            db._exec(stmt)
    fired = bondmod.sweep_bond_outreach(db)
    print(json.dumps({"ok": True, "at": now(), "db": path,
                      "outreach_sent": len(fired),
                      "fired": [{"fm_id": f, "type": t}
                                for f, t in fired]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
