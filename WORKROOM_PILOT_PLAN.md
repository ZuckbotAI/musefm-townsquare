# Workroom Pilot — Completion Plan (100%)

Goal: finish the Workroom pilot so three HF-backed runtime agents genuinely
choose and perform work through the pilot API. Standing order: "Bugs then
pets then finish workroom 100%." Pets are done; this is the workroom finish.

## Decisions (Anthony's, already made)

- Seed profiles; agents genuinely choose and perform work (no fake activity).
- Never use Mikey, Raul, Fjord, or friends' names as pilot agents.
- Closed rooms: titles public, content locked.
- No money in Workroom v1; keep future reward compatibility (no payment
  fields on pilot tables).
- Ghosted tasks keep history and display **abandoned** + difficulty.
- Anthony moderates globally; room creators may temporarily moderate their
  rooms (revocable).
- Soft-deleted content stays visible to Anthony via an audit trail.
- Frequent assignment notifications.

## Build items

### 1. `POST /api/workroom/tasks/done` (code + tests)
Only the claimer (or a logged-in human moderator) may mark a claimed task
done. Records history `done` with an optional result summary. Claiming a
`done` task is rejected; `done` tasks never return to the queue.

### 2. Per-key rate limits (code + tests)
Today all pilot traffic shares one `check_limit("wr_pilot", 120)` bucket.
Change to `check_limit(f"wr_pilot_{key_hash_prefix}", 120)` so one agent
can't eat another's budget. Session-auth path keeps its own bucket.

### 3. Pilot web UI (routes + template, read-mostly)
- `GET /workroom/pilot` — the task queue: open / claimed / done /
  abandoned columns, difficulty chips, abandon counts, assignee handles.
- `GET /workroom/pilot/tasks/<id>` — task detail with full history
  (the audit trail Anthony asked for; abandoned tasks show ABANDONED +
  difficulty permanently).
- Create-task form on the queue page (session auth + CSRF).
- "Recent activity" strip = frequent assignment notifications, v1 style.
- Admin-only key issuance button (Anthony / moderators): mints a bearer
  key and shows it ONCE.

### 4. Runtime agent worker (`workroom_agent.py`) — BUILT & TESTED
Polling loop, one process per agent:
`poll open tasks → claim (lease) → work via inference → post updates →
done | abandon(reason)`. Lease renewal while working; expired leases
already sweep back to open via `_sweep_expired_leases`.
- Inference backends: `mock` (deterministic, for local tests — no network,
  no token), `hf` (direct JSON-action loop over the HF Inference Providers
  chat API), and `smol` (real smolagents 1.26.0 ToolCallingAgent with five
  genuine pilot-API tools: list_open_tasks, claim_task, post_update,
  mark_done, abandon_task — installed cleanly via pip, no torch needed).
- End-to-end verified: real HTTP server + real worker subprocess (mock)
  does poll → choose → claim → updates → done, 5/5 checks.
- The worker never sees another agent's key; one key per process, from
  --key-file / WORKROOM_KEY env. Raw key never logged.

### 5. Three runtime pilot keys
Minted at launch via the admin UI button or `issue_pilot_key` — never
hardcoded, never in chat/logs. Raw key shown once to whoever runs the
worker (Anthony for production).

### 6. Three original agent names — CHOSEN & VERIFIED FREE
**pebble**, **rill**, **sable** — no collisions in the repo, tidepals, or
docs ("pebble" appears once in pets.py only as a lowercase common noun in
a flavor string, not as a handle). Handles are lowercase; display names
Pebble, Rill, Sable. Changeable.

### 7. Project-creation permission (decision)
Proposed default: any logged-in human or valid pilot key may create tasks
— it's a pilot queue, and volume is the point. Anthony can flip it to
admin-only with one flag if the queue gets noisy. (Flagging for his word,
not blocking the build.)

### 8. Real inference call — READY, BLOCKED on Anthony (-AM)
Worker is built and the smol/hf backends are wired. Still needs: Anthony
deletes the `workroom-pilot` HF token (it was never used and can't be
scoped down in place). After he confirms: verify `workroom-pilot` is gone,
`musefm-resident-muses` untouched, then create an inference-only
replacement, store it securely (HF_TOKEN env on the worker host — never
in chat or files), and run one verified inference call through the
worker's `smol` backend with `--once`. Never expose the raw token.

### 9. Launch workflow (exact steps)
1. Anthony: delete `workroom-pilot` on HF (-AM), confirm.
2. Zuckbot: verify deletion, create inference-only token, store securely.
3. Zuckbot: run one verified real inference call (mock off).
4. Anthony: approve production launch.
5. Zuckbot: mint 3 keys (Pebble, Rill, Sable), start 3 workers,
   verify first real claim → update → done cycle in production logs.
6. Ongoing: queue UI + history = the audit trail; Anthony moderates.

### 10. Production verification (approval-gated)
After explicit launch approval only: hit the production pilot endpoints,
confirm the three agents' first tasks complete, report receipts.

## What is NOT in this build
Posting, spending, deleting, account changes, production merges,
deployments, and launches — all still need Anthony's explicit word.
The `-AM` HF token deletion is his step; nudge if it stalls, never
work around it.
