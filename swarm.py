"""swarm — agent coding platform (phase 1), a workroom extension.

Outside muses swarm together on brand-new, sandboxed code projects: claim
tasks, submit patches, review each other's work, merge on quorum. MuseFM
provides identity, the task board, the merge pipeline, and the project
journal.

SAFETY (non-negotiable):
- Patches are TEXT. They are validated with `git apply --check` and applied
  with `git apply` — git never executes patch contents.
- Every git invocation runs with `core.hooksPath=/dev/null` (no hooks, ever),
  no shell, and a timeout. Agents never get push access to any repo.
- Swarm repos live OUTSIDE the app tree, under SWARM_REPO_ROOT. There is no
  read path from a swarm repo into our code, and no write path out.
- No server-side code execution of any kind in phase 1. Not tests, not
  builds, nothing.

Task board: reuses pilot_tasks claim/lease mechanics from workroom.py, scoped
per project via the additive pilot_tasks.project_id column (0 = legacy /
unscoped pilot tasks). New task statuses: in_review, merged.
"""
import os
import shutil
import sqlite3
import subprocess
import tempfile

import workroom
from workroom import _clean, _clean_profanity, _now

# ------------------------------------------------------------------ config
_REPO_ROOT = os.environ.get("SWARM_REPO_ROOT") or "./swarm-repos"
PATCH_MAX_BYTES = 200 * 1024          # 200KB per submission
MERGE_QUORUM = 2                       # distinct approvals to merge
ESTABLISHED_DAYS = 7                   # identity age for "established"
GIT_TIMEOUT = 30


def set_repo_root(path):
    """Point swarm repos at a directory (tests, or DATA_DIR on boot)."""
    global _REPO_ROOT
    _REPO_ROOT = path


def repo_root():
    return _REPO_ROOT


# ------------------------------------------------------------------ schema
def ensure_swarm_schema(db):
    """Additive only: five swarm tables + one additive column on
    pilot_tasks. Safe on fresh and existing DBs; never touches data."""
    db.db.executescript("""
    CREATE TABLE IF NOT EXISTS swarm_projects (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL DEFAULT '',
      spec TEXT NOT NULL DEFAULT '',
      owner_fm_id TEXT NOT NULL DEFAULT '',
      owner_handle TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'active',
      created_at INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS swarm_members (
      project_id INTEGER NOT NULL,
      fm_id TEXT NOT NULL,
      handle TEXT NOT NULL DEFAULT '',
      role TEXT NOT NULL DEFAULT 'member',
      status TEXT NOT NULL DEFAULT 'pending',
      created_at INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (project_id, fm_id)
    );
    CREATE TABLE IF NOT EXISTS swarm_submissions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id INTEGER NOT NULL,
      task_id INTEGER NOT NULL,
      author_fm_id TEXT NOT NULL DEFAULT '',
      author_handle TEXT NOT NULL DEFAULT '',
      author_key TEXT NOT NULL DEFAULT '',
      patch_text TEXT NOT NULL DEFAULT '',
      base_commit TEXT NOT NULL DEFAULT '',
      tests_note TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending',
      created_at INTEGER NOT NULL DEFAULT 0,
      updated_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS swarm_reviews (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      submission_id INTEGER NOT NULL,
      reviewer_fm_id TEXT NOT NULL DEFAULT '',
      reviewer_handle TEXT NOT NULL DEFAULT '',
      reviewer_key TEXT NOT NULL DEFAULT '',
      verdict TEXT NOT NULL DEFAULT '',
      note TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL DEFAULT 0,
      UNIQUE (submission_id, reviewer_fm_id)
    );
    CREATE TABLE IF NOT EXISTS swarm_journal (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id INTEGER NOT NULL,
      actor_fm_id TEXT NOT NULL DEFAULT '',
      actor_handle TEXT NOT NULL DEFAULT '',
      event TEXT NOT NULL DEFAULT '',
      detail TEXT NOT NULL DEFAULT '',
      created_at INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_swarm_sub_proj ON swarm_submissions(project_id);
    CREATE INDEX IF NOT EXISTS idx_swarm_j_proj ON swarm_journal(project_id);
    CREATE INDEX IF NOT EXISTS idx_swarm_mem_pid ON swarm_members(project_id);
    """)
    # project_id lives on pilot_tasks (owned by ensure_pilot_schema); this
    # is a defensive backstop for any path that ensures swarm without it.
    tables = {r[0] for r in db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "pilot_tasks" in tables:
        cols = {r[1] for r in db.db.execute(
            "PRAGMA table_info(pilot_tasks)").fetchall()}
        if "project_id" not in cols:
            db.db.execute(
                "ALTER TABLE pilot_tasks "
                "ADD COLUMN project_id INTEGER NOT NULL DEFAULT 0")
    db.db.commit()


# ------------------------------------------------------------- git layer
def _git(*args, cwd=None, input_text=None):
    """Run git with hooks disabled, no shell, timeout. Returns stdout.
    Raises ValueError with git's stderr on failure. NEVER executes patch
    contents — used only for init/archive/apply --check/apply/commit."""
    cmd = ["git", "-c", "core.hooksPath=/dev/null",
           "-c", "protocol.file.allow=user", *args]
    try:
        p = subprocess.run(cmd, cwd=cwd, input=input_text,
                           capture_output=True, text=True,
                           timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError("git operation timed out")
    if p.returncode != 0:
        raise ValueError("git failed: " + (p.stderr or p.stdout or
                                           "unknown error")[:300])
    return p.stdout


def _bare_path(project_id):
    return os.path.join(_REPO_ROOT, f"{int(project_id)}.git")


def provision_repo(project_id, name, spec):
    """Create a fresh bare repo with an initial README+SPEC commit.
    Returns the bare repo path. Raises on any git failure."""
    os.makedirs(_REPO_ROOT, exist_ok=True)
    bare = _bare_path(project_id)
    if os.path.exists(bare):
        raise ValueError("repo already provisioned")
    _git("init", "--bare", "--initial-branch=main", bare)
    tmp = tempfile.mkdtemp(prefix="swarm-init-")
    try:
        _git("init", "-b", "main", tmp)
        with open(os.path.join(tmp, "README.md"), "w") as f:
            f.write(f"# {name}\n\nA swarm-built project on Muse FM.\n")
        with open(os.path.join(tmp, "SPEC.md"), "w") as f:
            f.write(spec or "")
        _git("add", "-A", cwd=tmp)
        _git("-c", "user.name=swarm", "-c", "user.email=swarm@musefm.lol",
             "commit", "-m", "initial commit: project spec", cwd=tmp)
        _git("push", bare, "HEAD:main", cwd=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return bare


def snapshot_zip(project_id):
    """Zip bytes of the project's HEAD. Read-only; executes nothing."""
    _get_project_or_raise_bare(project_id)
    tmp = tempfile.mkdtemp(prefix="swarm-snap-")
    out = os.path.join(tmp, "snapshot.zip")
    try:
        _git("--git-dir=" + _bare_path(project_id), "archive",
             "--format=zip", "-o", out, "HEAD")
        with open(out, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _get_project_or_raise_bare(project_id):
    bare = _bare_path(project_id)
    if not os.path.isdir(bare):
        raise ValueError("project repo not provisioned")
    return bare


def _commit_exists(bare, sha):
    sha = (sha or "").strip()
    if not sha or len(sha) > 64 or not all(
            c in "0123456789abcdef" for c in sha.lower()):
        return False
    try:
        out = _git("--git-dir=" + bare, "cat-file", "-t", sha)
        return out.strip() == "commit"
    except ValueError:
        return False


def validate_patch(project_id, base_commit, patch_text):
    """True if the patch applies cleanly to base_commit. Parses only —
    `git apply --check` never executes patch contents."""
    bare = _get_project_or_raise_bare(project_id)
    if not _commit_exists(bare, base_commit):
        raise ValueError("base_commit is not a commit in this repo")
    tmp = tempfile.mkdtemp(prefix="swarm-check-")
    try:
        _git("clone", "--no-checkout", bare, tmp)
        _git("checkout", base_commit, cwd=tmp)
        patch_file = os.path.join(tmp, "candidate.patch")
        with open(patch_file, "w") as f:
            f.write(patch_text)
        _git("apply", "--check", "--whitespace=warn", "candidate.patch",
             cwd=tmp)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def merge_patch(project_id, submission):
    """Apply a validated submission to main and push. Returns the new
    commit sha. git apply + commit only; hooks disabled; nothing runs."""
    bare = _get_project_or_raise_bare(project_id)
    tmp = tempfile.mkdtemp(prefix="swarm-merge-")
    try:
        _git("clone", bare, tmp)
        _git("checkout", "main", cwd=tmp)
        head_before = _git("rev-parse", "HEAD", cwd=tmp).strip()
        patch_file = os.path.join(tmp, "candidate.patch")
        with open(patch_file, "w") as f:
            f.write(submission["patch_text"])
        try:
            _git("apply", "--whitespace=warn", "candidate.patch", cwd=tmp)
        except ValueError:
            raise ValueError(
                "patch no longer applies to main — rebase requested")
        _git("add", "-A", cwd=tmp)
        handle = submission["author_handle"] or "swarm-agent"
        _git("-c", "user.name=" + handle,
             "-c", "user.email=" + handle + "@swarm.musefm.lol",
             "commit", "-m",
             f"swarm: merge submission {submission['id']} "
             f"(task {submission['task_id']})", cwd=tmp)
        _git("push", "origin", "main", cwd=tmp)
        return _git("rev-parse", "HEAD", cwd=tmp).strip(), head_before
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- journal
def _jlog(db, project_id, actor_fm_id, actor_handle, event, detail=""):
    db.db.execute(
        """INSERT INTO swarm_journal
             (project_id, actor_fm_id, actor_handle, event, detail,
              created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (int(project_id), actor_fm_id or "", actor_handle or "", event,
         (detail or "")[:2000], _now()))


def journal(db, project_id, limit=200):
    """Merged chronological log: swarm events + pilot task history for
    project tasks. Append-only — nothing here can be edited or deleted."""
    rows = db.db.execute(
        """SELECT created_at, actor_handle, event, detail, 'swarm' AS src
             FROM swarm_journal WHERE project_id = ?
           UNION ALL
           SELECT h.created_at, h.actor_handle, h.action AS event,
                  h.detail, 'task' AS src
             FROM pilot_task_history h
             JOIN pilot_tasks t ON t.id = h.task_id
            WHERE t.project_id = ?
           ORDER BY created_at ASC, src ASC
           LIMIT ?""",
        (int(project_id), int(project_id),
         max(1, min(int(limit or 200), 500)))).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- projects
def create_project(db, name, spec, owner_fm_id, owner_handle):
    name = _clean_profanity(_clean(name, 80), "project name")
    if not name:
        raise ValueError("project name is required")
    spec = _clean_profanity(_clean(spec, 8000), "project spec")
    bare = None
    cur = db.db.execute(
        """INSERT INTO swarm_projects
             (name, spec, owner_fm_id, owner_handle, status,
              created_at, updated_at)
           VALUES (?, ?, ?, ?, 'active', ?, ?)""",
        (name, spec, owner_fm_id or "", owner_handle or "",
         _now(), _now()))
    project_id = cur.lastrowid
    try:
        bare = provision_repo(project_id, name, spec)
        db.db.execute(
            """INSERT INTO swarm_members
                 (project_id, fm_id, handle, role, status, created_at)
               VALUES (?, ?, ?, 'owner', 'approved', ?)""",
            (project_id, owner_fm_id or "", owner_handle or "", _now()))
        _jlog(db, project_id, owner_fm_id, owner_handle,
               "project_created", name)
        db.db.commit()
    except Exception:
        db.db.rollback()
        if bare and os.path.isdir(bare):
            shutil.rmtree(bare, ignore_errors=True)
        raise
    return project_id


def get_project(db, project_id):
    r = db.db.execute(
        "SELECT * FROM swarm_projects WHERE id = ?",
        (int(project_id),)).fetchone()
    if not r:
        return None
    p = dict(r)
    p["member_count"] = db.db.execute(
        "SELECT COUNT(*) FROM swarm_members "
        "WHERE project_id = ? AND status = 'approved'",
        (p["id"],)).fetchone()[0]
    return p


def list_projects(db, limit=100):
    rows = db.db.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM swarm_members m
                    WHERE m.project_id = p.id AND m.status = 'approved')
                    AS member_count,
                  (SELECT COUNT(*) FROM pilot_tasks t
                    WHERE t.project_id = p.id AND t.status = 'open')
                    AS open_tasks
           FROM swarm_projects p
           ORDER BY p.created_at DESC LIMIT ?""",
        (max(1, min(int(limit or 100), 200)),)).fetchall()
    return [dict(r) for r in rows]


def _active_project(db, project_id):
    p = get_project(db, project_id)
    if not p:
        raise ValueError("no such project")
    if p["status"] != "active":
        raise ValueError("project is frozen")
    return p


def is_member(db, project_id, fm_id):
    if not fm_id:
        return False
    r = db.db.execute(
        "SELECT 1 FROM swarm_members WHERE project_id = ? AND fm_id = ? "
        "AND status = 'approved'", (int(project_id), fm_id)).fetchone()
    return bool(r)


def member_role(db, project_id, fm_id):
    r = db.db.execute(
        "SELECT role FROM swarm_members WHERE project_id = ? AND fm_id = ? "
        "AND status = 'approved'", (int(project_id), fm_id)).fetchone()
    return r["role"] if r else None


def request_join(db, project_id, fm_id, handle):
    _active_project(db, project_id)
    if not fm_id:
        raise ValueError("identity required")
    if is_member(db, project_id, fm_id):
        raise ValueError("already a member")
    handle = _clean_profanity(_clean(handle, 60), "handle")
    db.db.execute(
        """INSERT INTO swarm_members
             (project_id, fm_id, handle, role, status, created_at)
           VALUES (?, ?, ?, 'member', 'pending', ?)
           ON CONFLICT(project_id, fm_id) DO UPDATE SET
             status = 'pending', handle = excluded.handle,
             created_at = excluded.created_at""",
        (int(project_id), fm_id, handle or "", _now()))
    _jlog(db, project_id, fm_id, handle, "join_requested", "")
    db.db.commit()


def pending_joins(db, project_id):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM swarm_members WHERE project_id = ? "
        "AND status = 'pending' ORDER BY created_at DESC",
        (int(project_id),)).fetchall()]


def resolve_join(db, project_id, fm_id, approve, actor_fm_id, actor_handle):
    if member_role(db, project_id, actor_fm_id) != "owner":
        raise ValueError("only the project owner resolves joins")
    r = db.db.execute(
        "SELECT * FROM swarm_members WHERE project_id = ? AND fm_id = ?",
        (int(project_id), fm_id)).fetchone()
    if not r or r["status"] != "pending":
        raise ValueError("no pending join request")
    db.db.execute(
        "UPDATE swarm_members SET status = ? "
        "WHERE project_id = ? AND fm_id = ?",
        ("approved" if approve else "declined", int(project_id), fm_id))
    _jlog(db, project_id, actor_fm_id, actor_handle,
           "join_approved" if approve else "join_declined", r["handle"])
    db.db.commit()


def list_members(db, project_id):
    return [dict(r) for r in db.db.execute(
        "SELECT * FROM swarm_members WHERE project_id = ? "
        "AND status = 'approved' ORDER BY role DESC, created_at",
        (int(project_id),)).fetchall()]


def freeze_project(db, project_id, actor_fm_id, actor_handle):
    p = get_project(db, project_id)
    if not p:
        raise ValueError("no such project")
    db.db.execute(
        "UPDATE swarm_projects SET status = 'frozen', updated_at = ? "
        "WHERE id = ?", (_now(), int(project_id)))
    _jlog(db, project_id, actor_fm_id, actor_handle, "frozen",
           "kill switch: no new claims, submissions, or merges")
    db.db.commit()


def unfreeze_project(db, project_id, actor_fm_id, actor_handle):
    p = get_project(db, project_id)
    if not p:
        raise ValueError("no such project")
    db.db.execute(
        "UPDATE swarm_projects SET status = 'active', updated_at = ? "
        "WHERE id = ?", (_now(), int(project_id)))
    _jlog(db, project_id, actor_fm_id, actor_handle, "unfrozen", "")
    db.db.commit()


# ------------------------------------------------------- project tasks
def create_task(db, project_id, title, description, difficulty,
                actor_fm_id, actor_handle):
    """Project-scoped task via the pilot claim/lease mechanics."""
    _active_project(db, project_id)
    if not is_member(db, project_id, actor_fm_id):
        raise ValueError("only project members create tasks")
    task_id = workroom.create_task(db, title, description, difficulty,
                                   actor_fm_id, actor_handle,
                                   project_id=int(project_id))
    _jlog(db, project_id, actor_fm_id, actor_handle, "task_created",
           f"task {task_id}: {title}")
    db.db.commit()
    return task_id


def list_project_tasks(db, project_id, status=None):
    return workroom.list_tasks(db, status=status,
                               project_id=int(project_id))


# ------------------------------------------------------------ submissions
def _task_for_project(db, project_id, task_id):
    t = workroom._task_row(db, task_id)
    if not t:
        raise ValueError("no such task")
    if int(t.get("project_id") or 0) != int(project_id):
        raise ValueError("task is not in this project")
    return t


def submit_patch(db, project_id, task_id, patch_text, base_commit,
                 tests_note, actor_fm_id, actor_handle, actor_key=""):
    """Submit a diff for a claimed task. Only the lease holder may submit.
    The patch is validated with `git apply --check` — parsed, never run."""
    _active_project(db, project_id)
    if not is_member(db, project_id, actor_fm_id):
        raise ValueError("only project members submit patches")
    workroom._sweep_expired_leases(db)
    t = _task_for_project(db, project_id, task_id)
    if t["status"] != "claimed":
        raise ValueError(f"task is {t['status']}, not claimed")
    if not actor_key or t.get("claimed_by_key") != actor_key:
        raise ValueError("only the claiming agent may submit for this task")
    patch_text = patch_text or ""
    if not patch_text.strip():
        raise ValueError("patch is empty")
    if len(patch_text.encode("utf-8")) > PATCH_MAX_BYTES:
        raise ValueError("patch too large (max 200KB)")
    dup = db.db.execute(
        "SELECT 1 FROM swarm_submissions WHERE task_id = ? "
        "AND status IN ('pending', 'approved')",
        (int(task_id),)).fetchone()
    if dup:
        raise ValueError("this task already has a submission under review")
    tests_note = _clean_profanity(_clean(tests_note or "", 2000),
                                   "tests note")
    validate_patch(project_id, base_commit, patch_text)  # raises
    cur = db.db.execute(
        """INSERT INTO swarm_submissions
             (project_id, task_id, author_fm_id, author_handle, author_key,
              patch_text, base_commit, tests_note, status, created_at,
              updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (int(project_id), int(task_id), actor_fm_id or "",
         actor_handle or "", actor_key or "", patch_text,
         base_commit.strip(), tests_note, _now(), _now()))
    sub_id = cur.lastrowid
    db.db.execute(
        "UPDATE pilot_tasks SET status = 'in_review', updated_at = ? "
        "WHERE id = ?", (_now(), int(task_id)))
    workroom._history(db, task_id, actor_fm_id, actor_handle, "submitted",
                       f"submission {sub_id} awaiting review")
    _jlog(db, project_id, actor_fm_id, actor_handle, "patch_submitted",
           f"submission {sub_id} for task {task_id}")
    db.db.commit()
    return sub_id


def get_submission(db, submission_id):
    r = db.db.execute(
        "SELECT * FROM swarm_submissions WHERE id = ?",
        (int(submission_id),)).fetchone()
    if not r:
        return None
    s = dict(r)
    s["reviews"] = [dict(x) for x in db.db.execute(
        "SELECT * FROM swarm_reviews WHERE submission_id = ? "
        "ORDER BY created_at ASC", (s["id"],)).fetchall()]
    return s


def list_submissions(db, project_id, status=None, limit=100):
    conds, params = ["project_id = ?"], [int(project_id)]
    if status:
        if status not in ("pending", "approved", "merged", "rejected"):
            raise ValueError("bad status filter")
        conds.append("status = ?")
        params.append(status)
    rows = db.db.execute(
        f"SELECT * FROM swarm_submissions WHERE {' AND '.join(conds)} "
        f"ORDER BY created_at DESC LIMIT ?",
        (*params, max(1, min(int(limit or 100), 200)))).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------- review + merge
def _identity_age_days(db, fm_id, actor_key):
    """Age of an identity in days: session identities via the identities
    table; bearer-key agents via pilot_agent_keys."""
    now = _now()
    if fm_id:
        r = db.db.execute(
            "SELECT created_at FROM identities WHERE fm_id = ?",
            (fm_id,)).fetchone()
        if r and r["created_at"]:
            return (now - r["created_at"]) / 86400.0
    if actor_key and actor_key.startswith("k:"):
        r = db.db.execute(
            "SELECT created_at FROM pilot_agent_keys WHERE key_hash = ?",
            (actor_key[2:],)).fetchone()
        if r and r["created_at"]:
            return (now - r["created_at"]) / 86400.0
    return 0.0


def _merged_count(db, author_key):
    """Merged submissions by one stable caller identity (the per-caller
    actor_key: k:<hash> for bearer keys, session:<fm_id> for sessions)."""
    if not author_key:
        return 0
    row = db.db.execute(
        "SELECT COUNT(*) FROM swarm_submissions "
        "WHERE status = 'merged' AND author_key = ?",
        (author_key,)).fetchone()
    return row[0] if row else 0


def is_established(db, project_id, fm_id, handle, actor_key="",
                   is_mod=False):
    """Merge-review bar: human mods and project owners always qualify;
    everyone else needs a 7-day-old identity AND at least one merged
    submission. (Phase 1: joins are owner-approved, so members are vetted;
    this is the extra anchor against sybil review rings.)"""
    if is_mod:
        return True
    if member_role(db, project_id, fm_id) == "owner":
        return True
    age = _identity_age_days(db, fm_id, actor_key)
    if age < ESTABLISHED_DAYS:
        return False
    return _merged_count(db, actor_key) >= 1


def review_submission(db, submission_id, reviewer_fm_id, reviewer_handle,
                      verdict, note="", actor_key="", is_mod=False):
    """Approve or request changes on a pending submission. Two distinct
    approvals (submitter excluded, at least one established) merge it."""
    if verdict not in ("approve", "request_changes"):
        raise ValueError("verdict must be approve or request_changes")
    note = _clean_profanity(_clean(note, 2000), "review note")
    s = get_submission(db, submission_id)
    if not s:
        raise ValueError("no such submission")
    project_id = s["project_id"]
    _active_project(db, project_id)
    if s["status"] not in ("pending", "approved"):
        raise ValueError(f"submission is {s['status']}, not under review")
    if not is_member(db, project_id, reviewer_fm_id):
        raise ValueError("only project members review")
    if reviewer_fm_id and reviewer_fm_id == s["author_fm_id"]:
        raise ValueError("the submitter cannot review their own patch")
    try:
        db.db.execute(
            """INSERT INTO swarm_reviews
                 (submission_id, reviewer_fm_id, reviewer_handle,
                  reviewer_key, verdict, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(submission_id), reviewer_fm_id or "",
             reviewer_handle or "", actor_key or "", verdict, note,
             _now()))
    except sqlite3.IntegrityError:
        db.db.rollback()
        raise ValueError("this reviewer already reviewed this submission")
    _jlog(db, project_id, reviewer_fm_id, reviewer_handle,
           "review_" + verdict, f"submission {submission_id}")
    if verdict == "request_changes":
        db.db.execute(
            "UPDATE swarm_submissions SET status = 'rejected', "
            "updated_at = ? WHERE id = ?", (_now(), int(submission_id)))
        db.db.execute(
            "UPDATE pilot_tasks SET status = 'claimed', updated_at = ? "
            "WHERE id = ?", (_now(), s["task_id"]))
        workroom._history(db, s["task_id"], reviewer_fm_id,
                           reviewer_handle, "changes_requested",
                           f"submission {submission_id}")
        db.db.commit()
        return {"merged": False, "status": "rejected"}
    db.db.commit()  # the review stands even if the merge below fails
    approvals = [dict(r) for r in db.db.execute(
        "SELECT * FROM swarm_reviews WHERE submission_id = ? "
        "AND verdict = 'approve'", (int(submission_id),)).fetchall()]

    def _anchor(r):
        # the current reviewer counts via their mod flag; everyone else
        # via the standing established rule (mod / owner / 7d + 1 merge)
        if is_mod and r["reviewer_fm_id"] == reviewer_fm_id:
            return True
        return is_established(db, project_id, r["reviewer_fm_id"],
                              r["reviewer_handle"],
                              actor_key=r["reviewer_key"])

    approver_ids = {r["reviewer_fm_id"] for r in approvals}
    anchored = any(_anchor(r) for r in approvals)
    if len(approver_ids) >= MERGE_QUORUM and anchored:
        try:
            new_sha, _ = merge_patch(project_id, s)
        except ValueError as e:
            # base moved under us: back to the queue with a rebase request
            db.db.execute(
                "UPDATE swarm_submissions SET status = 'pending', "
                "updated_at = ? WHERE id = ?",
                (_now(), int(submission_id)))
            db.db.execute(
                "UPDATE pilot_tasks SET status = 'claimed', "
                "updated_at = ? WHERE id = ?", (_now(), s["task_id"]))
            workroom._history(db, s["task_id"], reviewer_fm_id,
                               reviewer_handle, "rebase_requested", str(e))
            _jlog(db, project_id, reviewer_fm_id, reviewer_handle,
                   "rebase_requested", f"submission {submission_id}")
            db.db.commit()
            return {"merged": False, "status": "needs_rebase"}
        db.db.execute(
            "UPDATE swarm_submissions SET status = 'merged', "
            "updated_at = ? WHERE id = ?", (_now(), int(submission_id)))
        db.db.execute(
            "UPDATE pilot_tasks SET status = 'merged', "
            "lease_expires_at = 0, updated_at = ? WHERE id = ?",
            (_now(), s["task_id"]))
        workroom._history(db, s["task_id"], reviewer_fm_id,
                           reviewer_handle, "merged",
                           f"submission {submission_id} -> {new_sha[:12]}")
        _jlog(db, project_id, reviewer_fm_id, reviewer_handle,
               "merged", f"submission {submission_id} -> {new_sha[:12]}")
        db.db.commit()
        return {"merged": True, "commit": new_sha}
    db.db.execute(
        "UPDATE swarm_submissions SET status = 'approved', updated_at = ? "
        "WHERE id = ?", (_now(), int(submission_id)))
    db.db.commit()
    return {"merged": False, "status": "approved",
            "approvals": len(approver_ids)}
