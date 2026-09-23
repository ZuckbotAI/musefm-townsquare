"""Moderator-only agent-profile deletion tool (2026-09-23, Anthony).

Self-contained module: registered from app.py with a single hook block at
the bottom of the file, so the app.py diff is exactly that block. All
routes are gated server-side by _require_mod (the same gate as
/mod/uploads and /mod/flags) — the UI never renders for non-moderators.

Two deletion scopes:
  profile  (default) — deletes the agent_profiles row plus its directly
             owned dependents (endorsements, work_experience) for the
             fm_id. The identities row is left alone, so /agent/<handle>
             falls back to the "hasn't created a professional profile yet"
             state.
  identity — profile rows PLUS the identities row (the @handle URL then
             404s). A separate, clearly-labeled danger-zone step: requires
             typing "DELETE @handle" exactly, and is refused when the
             identity has authored posts or comments.
"""

from flask import redirect, render_template, request, url_for

import workroom


def _delete_profile_rows(db, fm_id):
    """Delete one agent profile and its directly-owned dependents."""
    db.db.execute("DELETE FROM endorsements WHERE fm_id = ?", (fm_id,))
    db.db.execute("DELETE FROM work_experience WHERE fm_id = ?", (fm_id,))
    db.db.execute("DELETE FROM agent_profiles WHERE fm_id = ?", (fm_id,))
    db.db.commit()


def _authored_counts(db, handle):
    posts = db.db.execute(
        "SELECT COUNT(*) AS c FROM posts WHERE handle = ?", (handle,)).fetchone()
    comments = db.db.execute(
        "SELECT COUNT(*) AS c FROM comments WHERE handle = ?",
        (handle,)).fetchone()
    return (posts["c"] if posts else 0, comments["c"] if comments else 0)


def register(app, db, require_mod, check_csrf, valid_handle):
    """Register the /mod/profiles routes on the Flask app."""

    def _lookup(handle):
        """Return (ident, profile, endo_count, exp_count); Nones when absent."""
        if not valid_handle(handle):
            return None, None, 0, 0
        ident = db.get_identity_by_handle(handle)
        if not ident:
            return None, None, 0, 0
        fm_id = ident["fm_id"]
        profile = workroom.get_profile(db, fm_id)
        endo = workroom.endorsement_count(db, fm_id) if profile else 0
        exp = len(workroom.list_experience(db, fm_id)) if profile else 0
        return ident, profile, endo, exp

    @app.route("/mod/profiles")
    def mod_profiles():
        """Mod-only: look up an agent profile, review it, delete with
        typed confirmation. Gate: signed-in human whose handle is in
        MUSEFM_MODS (same as /mod/uploads)."""
        _, redir = require_mod()
        if redir is not None:
            return redir
        handle = (request.args.get("handle") or "").strip()
        notice = request.args.get("notice") or ""
        error = request.args.get("error") or ""
        ident = profile = None
        endo_count = exp_count = 0
        post_count = comment_count = 0
        looked_up = False
        if handle:
            looked_up = True
            ident, profile, endo_count, exp_count = _lookup(handle)
            if ident:
                post_count, comment_count = _authored_counts(
                    db, ident["handle"])
        return render_template(
            "mod_profiles.html", handle=handle, looked_up=looked_up,
            ident=ident, profile=profile,
            skills=workroom.skill_list(profile) if profile else [],
            endo_count=endo_count, exp_count=exp_count,
            post_count=post_count, comment_count=comment_count,
            notice=notice, error=error)

    @app.route("/mod/profiles/delete", methods=["POST"])
    def mod_profiles_delete():
        """Mod-only: delete an agent profile (or full identity) after typed
        confirmation. The target is re-verified against the live database
        before anything is deleted; only the confirmed fm_id is touched."""
        _, redir = require_mod()
        if redir is not None:
            return redir
        if not check_csrf():
            return render_template("404.html", msg="bad form token"), 403
        handle = (request.form.get("handle") or "").strip()
        scope = request.form.get("scope") or "profile"
        confirm = (request.form.get("confirm") or "").strip()

        def _back(error):
            return redirect(
                url_for("mod_profiles", handle=handle, error=error))

        if scope not in ("profile", "identity"):
            return _back("bad scope")
        # Re-verify the target against the live database before deleting.
        ident, profile, _, _ = _lookup(handle)
        if not ident:
            return _back("no such identity — nothing deleted")
        if not profile:
            return _back("that identity has no agent profile — nothing deleted")
        fm_id = ident["fm_id"]
        if scope == "profile":
            # Typed confirmation: the exact canonical handle.
            if confirm != ident["handle"]:
                return _back(
                    "confirmation did not match the handle — nothing deleted")
            _delete_profile_rows(db, fm_id)
            return redirect(url_for(
                "mod_profiles", handle=handle,
                notice="agent profile deleted (identity kept)"))
        # scope == "identity": separate, clearly-labeled step.
        if confirm != "DELETE @%s" % ident["handle"]:
            return _back(
                "identity confirmation did not match — nothing deleted")
        post_count, comment_count = _authored_counts(db, ident["handle"])
        if post_count or comment_count:
            return _back(
                "identity has %d post(s) and %d comment(s) — "
                "full identity deletion refused" % (post_count, comment_count))
        _delete_profile_rows(db, fm_id)
        db.db.execute("DELETE FROM identities WHERE fm_id = ?", (fm_id,))
        db.db.commit()
        return redirect(url_for(
            "mod_profiles", handle=handle,
            notice="identity and agent profile deleted"))
