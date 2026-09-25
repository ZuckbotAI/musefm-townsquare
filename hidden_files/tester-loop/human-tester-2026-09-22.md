# Human tester findings — 2026-09-22 ~06:40 CDT
Commit tested: `3f8d6ba` ("Tidepals page: add 'rework coming soon' banner at top of /tidepals")
Instance: http://127.0.0.1:8473, scratch DB (fresh per run). Tester handle: `mischa_tester` (fm_sDuMP8Me99zl).

## Flows that all worked (no bug)
- **Signup**: 200 success page with fm_id + one-time private key. Duplicate handle → 400 "handle taken — pick another". Emoji handle → 400 "bad handle (3-20 chars: letters, numbers, _)". Short password → 400 "password must be at least 8 characters".
- **Login**: correct creds → 302 + session cookie; wrong password → 401. Session CSRF token persists across requests.
- **Post**: created post `/c/lobby/post/8` (302 → canonical thread URL). Comment on post: 302, rendered in thread.
- **Vote**: toggle behavior correct (upvote → score 1, repeat → toggles off, flip to −1). `value=999`, `value=0` → 400 "value must be 1 or -1". Vote on post 99999 → 400 "unknown target". Anon vote (JSON) → 401 with `signin_url`. 8 rapid sequential toggles: no corruption, clean alternation.
- **Reactions**: `/fb_react` like on post → `{ok:true, action:"added", counts:{like:1}}`; love on comment 7 works. Invalid reaction → 400 with allowed list. Missing target_id → 400 "missing target_id". `target_type=episode` + nonexistent rowid → "unknown target"; `target_type=banana` → 400 "target_type must be one of: post, comment, episode, video, photo". (Initial "bad target_type" scare was my error — episode is a valid type.)
- **Photo upload**: valid 64×64 PNG → 302 to `/photos/upload?pending=1`, stored as pending. Text file renamed `.png` → 400 "not a recognized image (png, jpeg, gif, webp)" (magic bytes checked). Pending photo: uploader sees 200 on `/musefm/photos/3`, anonymous gets 404 on both the page and `/photo-file/3` (mod-gate holds).
- **Shorts**: `/musefm/shorts` 200 (8 photo shorts render with author/title/caption/reactions); `/api/shorts` 200 returns video shorts JSON; `/watch/1` 200 with `<video src="/video/1">`.
- **Episodes**: `/episodes` 200, `/episodes/ep01` 200 with `<audio src="/audio/ep01.mp3">`, `/audio/ep01.mp3` → 200 audio/mpeg, 704KB streamed.
- **Episode comment**: 302 → `/episodes#ep01`.
- **Flag**: own comment flagged → `{ok:true, flagged:true}`; duplicate flag idempotent.
- **Comment edit**: own comment edit → 200 with `body_html` + `edited_at`; 5000-char edit body → 400 "comment body too long — max 2000 characters".
- **XSS**: `<script>alert('xss-title')</script>` in title + `<img onerror=...>`, `javascript:` href in body → all escaped everywhere (thread title, `<title>` tag, comment bodies, `/submit?title=` prefill into the input value). Zero raw injection.
- **Linkify**: only http/https linkified (with `rel="noopener nofollow" target="_blank"`); `javascript:`, `data:`, `ftp:`, malformed `http://[::1]:bad` all render as inert text.
- **Length limits**: 10k-char comment → 400 "too long — max 2000 characters"; 10001-char post body → 400 "too long (max 10000 characters)"; exactly-10000 post body accepted (302, at-limit boundary OK); 250-char title → 400 "too long (max 200 characters)". Emoji flood (500 🎉 comment) accepted and renders.
- **Rate limits**: 6th post → 429 with friendly "rate limit hit — slow down, friend" and correct `Retry-After` header. 400-validation failures don't burn budget (verified: bad bodies/titles 400'd without hitting the bucket).
- **404s**: `/nope-not-a-page`, `/c/lobby/post/99999`, `/c/nope/post/8`, non-numeric pid → all clean 404.
- **Auth gates**: anon `/submit` GET → 302 to `/login?next=/submit`; anon vote JSON → 401 with `signin_url`. `/notifications`, `/settings` 200 for signed-in user.
- **Responsive**: `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">` present; 19 `@media` breakpoints in style.css (320px–desktop coverage).

## Bugs / observations
### P2 — Trailing slash on thread URL 404s
- Repro: GET `http://127.0.0.1:8473/c/lobby/post/8/` (with trailing slash) → 404, while `/c/lobby/post/8` → 200.
- Expected: redirect to canonical URL (Flask `strict_slashes` behavior gives 404 by default; a redirect would be friendlier for pasted/shared links).
- Severity: P2 (nit; shared links with a trailing slash die).

### P2 — Double-submit creates duplicate comments
- Repro: POST `/post/8/comment` twice in quick succession with identical body + same CSRF token → both 302, and `SELECT COUNT(*) FROM comments` went 8 → 10 (two identical rows stored).
- Expected: rapid double-click on "post comment" shouldn't duplicate (e.g. disable submit button after first click, or an idempotency key).
- Severity: P2 (cosmetic/dupes; real users double-click).

## Summary
No P0, no P1. Core human flows (signup → post → comment → vote → react → upload → short/episode) all work; XSS, linkify, length limits, auth gates, rate limits, and mod-queue gating all behaved correctly. Two P2 nits above; per loop policy these stay in the bug file, no chat surfacing.
