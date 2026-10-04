"""'Okay by me' approval policy helpers (2026-10-04, overhaul item 14).

DRAFT: not imported by app.py, no live path uses this yet. Anthony
approves the per-action policy before anything reads it in production.

Fail-open is deliberate: an action with no policy row is treated as NOT
requiring approval. A missing row must never silently queue everything.
"""
import time

from db import APPROVAL_POLICY_DEFAULTS  # noqa: F401  (documents the seed)


def requires_approval(db, action):
    """True when the named action must wait for owner approval."""
    row = db._one(
        "SELECT requires_approval FROM approval_policy WHERE action=?",
        (action,))
    if row is None:
        return False
    return bool(row["requires_approval"])


def set_policy(db, action, required, notes=""):
    """Set (or add) the policy for an action. For the future admin UI."""
    db._exec(
        "INSERT INTO approval_policy"
        " (action, requires_approval, notes, updated_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(action) DO UPDATE SET"
        " requires_approval=excluded.requires_approval,"
        " notes=excluded.notes,"
        " updated_at=excluded.updated_at",
        (action, 1 if required else 0, notes, int(time.time())))


def get_policy(db):
    """Full policy table as {action: (requires_approval, notes)}."""
    return {r["action"]: (bool(r["requires_approval"]), r["notes"])
            for r in db._q("SELECT action, requires_approval, notes"
                           " FROM approval_policy ORDER BY action")}
