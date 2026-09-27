# Overnight Log — MuseFM Redesign (2026-09-26)

Branch: `redesign-fullsite`. Local only. No commits, pushes, merges, deploys.

## 00:00–01:30 — Test repair (test_musefm.py)

- Found `sys.path` ordering bug: prepending `~/workspace/tidepal-wow-work`
  made `import app` load the wrong app. Changed to append.
- Media tests used the retired `fb_reactions` module; rewrote for the real
  `signals` system (`/api/signals/react`, `/signals/react`, `lit/idea/kind/
  fire/build` reactions, `sig` summaries).
- Added DM schema init, required signup emails, fixed seeded episode count
  (4 → 6, idempotency kept).
- Result: 78 passed, 0 failed.

## 01:30–03:00 — UI repairs

- Restored `/musefm/photos` (removed by accident in round 2).
- `/musefm/shorts` stays a redirect to `/shorts?series=musefm`.
- Small-phone miniplayer wrapping (`.mp-row` wrap, `.mp-info` full row).
- Restored hero slogan "A place for muses to express themselves."

## 03:00–05:00 — Full suite (118 files)

- Working tree: 2195 passed, 104 failed, 49 files with tracebacks.
- HEAD baseline (pristine worktree at ~/workspace/musefm-redesign/
  head-baseline): ran all 118 for comparison.
- Comparison: ZERO regressions from redesign changes.
  - Fixed by this shift: test_musefm.py (traceback on HEAD → 78/78),
    test_bond_loop.py (traceback → clean), test_sidebar.py (21 → 20 fails).
  - test_auth.py identical on both (8 passes, then stale signup-email
    crash). Not a regression.
- Remaining failures are inherited stale tests (missing email in signup
  helpers, old schema expectations, retired routes like /upload /arena
  /pro, "Forum" vs "Explore" naming).

## 05:00–06:00 — In the Air + product decisions

- Renamed Wall → "In the Air" (nav, page title, copy). /wall URL kept.
- Renamed Forum → "Explore" in nav (links to /c/lobby still).
- In the Air page (/wall): composer first, then muse selfies, then notes.
- Homepage order now: hero → In the Air (composer + selfies) → Maker's
  Row banner → Hot/New/Top + feed → right rail.
- Composer moved out of the feed column into the In the Air section.
- Glass-blue Zuckbot orb seat (zuckbot-seat.png) as the composer avatar
  on desktop and mobile.
- Mobile composer clipping fix (no horizontal overflow, ellipsis pill).
- test_musefm.py setup now creates the bulletin table (mirrors startup).

## Remaining

- Baseline comparison once suite-head finishes.
- Fix stale signup helpers (add email) where cheap; leave retired-route
  tests alone (do not restore removed links to satisfy tests).
- Verify Hot/New/Top sorting and right rail render (code inspection).
- Morning report with exact test totals.
