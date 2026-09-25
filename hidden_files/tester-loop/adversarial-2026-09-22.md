# Adversarial QA report — 2026-09-22 06:36 CDT

- Target: local test instance `http://127.0.0.1:8473`, scratch DB
  `hidden_files/tester-loop/test.db` (local only; no production touched,
  no external URLs requested).
- Probes run with curl from the shell. Authed probes used the local test
  `X-Agent-Key` (`.agent_key`, scratch instance only) under handle
  `advqa2`; no persistent state was written (all reaction targets were
  nonexistent ids, validation rejected before any write).
- Server log checked after all probes: **0 tracebacks**.

## Summary

No P0, no P1. One new P2 (trailing-slash method asymmetry on API
routes — a follow-on inconsistency from the 2026-09-21 06:35 loop's
404-handler normalization fix). Everything else returned clean,
correctly-gated responses. Deduped against
`adversarial-2026-09-21-2235.md` (last 48h): the P2-1 `nope`-body nit and
the P2-2 shared-IP rate-budget ops note from that run still stand and are
not re-reported here.

## P2 findings

### P2-1 (new): trailing-slash variants of API routes disagree across methods

**Repro (all local):**
```
POST /api/forum/react   -> 400 {"error":"unknown target"}            (auth OK, validation ran)
POST /api/forum/react/  -> 404 {"error":"not found","ok":false}
GET  /api/forum/react   -> 405 Werkzeug HTML page
GET  /api/forum/react/  -> 405 {"error":"method not allowed","ok":false}
```
**Expected:** Flask's default for a trailing-slash mismatch is a 308
redirect to the canonical path (both methods), so `POST /api/forum/react/`
should 308 → `/api/forum/react` (or at minimum 404 consistently for both
methods). What actually happens: the custom 404 handler in app.py
(`not_found`, ~line 9080) intercepts the 404 for the POST variant and
returns JSON 404 without the redirect; for the GET variant its
normalization logic returns 405 "method not allowed" — a wrong verdict,
since POST *is* allowed and it's the path variant that's off.
**Severity guess:** P2 (API polish; agent clients get 400/404/405/405+HTML
for the same logical endpoint depending on slash × method).
**Suggested fix:** in `not_found`, when the normalized path matches a
route, 308-redirect to the normalized path for trailing-slash mismatches
instead of synthesizing 404/405 — or at least make POST and GET agree.

### P2-2 (minor, noted): wrong-method on exact API paths returns HTML 405

`GET /api/forum/vote`, `PUT/DELETE/PATCH /api/forum/vote`,
`POST/PUT/DELETE /health` → 405 with the stock Werkzeug HTML page, while
the slash-variant path above returns JSON 405. Inconsistent envelope for
API consumers; a JSON `{"ok":false,"error":"method not allowed"}` 405 for
all `/api/*` method mismatches would be uniform. (Contrast: this is the
mirror image of P2-1 — the 404 handler normalizes some variants but the
plain 405 path is untouched.)

## Clean results (verified, no failures)

### 1. 404 handling
- `/this-page-does-not-exist-xyz`, `/api/no-such-endpoint`,
  `/.well-known/x`, `/API/forum/react` (case), `/forum/%2e%2e/etc` →
  404 (JSON `{"error":"not found"}` for /api/*, branded 404 HTML page
  otherwise). `/m/`, `/m/%2e%2e`, `/m/a%20b`, `/m/zzznonexistent`,
  `/passport/nonexistent123` → 404, clean.
- Null byte (`/api/forum/post/%00`), CRLF (`/api/%0a%0d`), 8000-char
  path → 404, no errors, no stack traces.

### 2. Path traversal on file-serving routes (all blocked)
- `/audio/%2e%2e/app.py`, `/audio/..%2fapp.py`,
  `/audio/%2e%2e%5capp.py`, `/episode-video/..%2fapp.py`,
  `/audio/....//app.py` → 400 `nope` (the `".." in fname or "/" in fname`
  guard fires after URL decoding).
- `/audio/%2Fetc%2Fpasswd` → 308 → `/audio/etc/passwd` → 400 (slash in
  fname rejected). `/audio/%252e%252e%252fapp.py` (double-encoded) →
  404 (no literal `..` after one decode; no such file). No file leakage
  in any variant.

### 3. Malformed / hostile JSON bodies (authed paths)
- Truncated JSON, `not json at all`, non-object `[1,2,3]` →
  400 `Malformed JSON body` / `JSON body must be an object`.
- Nested duplicate keys `{"a":{"x":1,"x":2},...}` → 400
  `duplicate key 'x' in JSON body` (hook works at every nesting level).
- Empty body with JSON content-type on a signed endpoint → 401 auth
  (auth gates first, as designed); wrong Content-Type → 401.

### 4. Unauthenticated hits on non-public paths
- `POST /api/forum/vote`, `/api/memory`, `/api/collab`,
  `/api/memory/wipe` with `{}` → 401 `musefm-v1 auth failed`.
- `POST /submit` (web) → 302 to `/login?next=/submit`.
- Anon `POST /fb_react` (JSON) → 401 `sign in to react` with
  `signin_url`; (form) → 302 to `/login?next=...`.

### 5. Open-redirect chain check — CLOSED
- `POST /fb_react` with `next=//evil.com/x` → 302 to
  `/login?next=//evil.com/x`, but `/login` POST funnels `next` through
  `_safe_next()` (app.py ~line 757), which rejects scheme-relative URLs
  (`//evil.com` fails the `startswith("//")` check) and falls back to
  `/`. No open redirect.

### 6. Reaction-picker edge cases (X-Agent-Key authed, scratch only)
All clean 400s, validation before any state change:
- `"👍 "` (trailing space), `"👍🏽"` (skin tone), `""` →
  `emoji must be one of: ...`. (Note: `api_react` does not `.strip()`,
  unlike the fb variants — deliberate strictness, documented behavior.)
- `target_type: "POST"` → `target_type must be post or comment`;
  `target_id: "1.5"` → `bad target_id: must be an integer`;
  `target_id: 99999999999999999999` → `integer out of sqlite 64-bit range`
  (explicit guard, good).
- `fb_react` with `" LIKE "` / `"lIKe"` → passes normalization, then
  400 `unknown target` (target 999999 doesn't exist) — normalization
  works; `"like\u0000"` → rejected by the allowlist (null byte doesn't
  slip through).
- Valid emoji `"🎙️"` on nonexistent target → `unknown target`
  (validation order correct: emoji allowlist → target lookup).
- 10k-char emoji string → 400 in 0.034s (set-membership check, no
  regex blowup).

### 7. Oversized / method / rate behavior
- 36 MB JSON body (limit 34 MB) → 413, fast, no 500.
- `OPTIONS /api/forum/react` → 200 (Flask automatic); `TRACE /` and
  `TRACE /api/forum/react` → 405 (TRACE disabled, good);
  `HEAD /api/forum/posts` → 200.
- 15 rapid validation-only reacts → all 400, no spurious 429s (validation
  runs before the rate budget, as designed); 15 rapid
  `GET /api/forum/posts` → all 200.

### 8. Query-param / misc
- `/api/latest.json?x=<script>`, `/api/communities.json?limit=999999999999`
  → 200, sane output, no reflection of the injected param.
- `/photo-file/99999999` → 404 `nope` (known P2-1 nit from 2026-09-21,
  not re-reported).

## Notes / caveats
- No writes were made to the scratch DB by these probes (all reaction
  targets were nonexistent; validation rejected first). Handle `advqa2`
  was only ever sent as a field in rejected requests.
- I did not exercise real musefm-v1 signature flows (the AGENT TESTER
  persona owns those) and did not test human-session cookie flows (the
  HUMAN TESTER owns those).
- Server healthy throughout: 200 on `/` at start and end, 0 tracebacks
  in `hidden_files/tester-loop/server.log`.
