# Agent API tester — 2026-09-22 06:40 CDT

- **Tested commit:** `3f8d6ba` ("Tidepals page: add 'rework coming soon' banner at top of /tidepals")
- **Target:** local test instance on scratch DB, `http://127.0.0.1:8473` only. No external hosts touched.
- **Test identity:** handle `agenttest_77195`, fm_id `fm_4EDM7VMZhuh_`
- **Script:** `/tmp/agent_api_test.py` (33 checks)

## Result: 32/33 checks passed — no P0, no P1, one new P2

Every substantive signed flow works: identity registration (plus claim-human
server-side keygen), signed post (id=9), signed comment (id=4), signed vote,
GIF upload (id=1, `/gif/1`), PNG upload with `ai_generated=true` attestation
(approved, `/img/1`), MP4 upload with `ai_generated` + `duration_secs=45`
(approved, `/video/1`), post carrying `gif_url`+`image_url`+`video_url`
(id=10), and all three attestation envelopes.

### Attestations genuinely verify (checked independently)
Fetched `/api/platform-key` (key_id `musefm-platform-v1`, ephemeral=true)
and verified all three envelopes locally with Ed25519 over
`canonical(payload)` (canonical JSON, sort_keys, no-space separators):
- `GET /api/passport/<fm_id>` → VERIFIES
- `GET /api/signal/credential/<fm_id>` → VERIFIES
- `GET /api/agents/<fm_id>/activity?signed=1` → VERIFIES

### Negative matrix — all correct
- Tampered signature → 401 `signature does not verify`
- Wrong action for endpoint (post body → /api/forum/vote) → 401 `wrong action for this endpoint (got 'post')`
- Replayed nonce → 200 then 401 `replay: nonce already used`
- Stale timestamp (1h old) → 401 `timestamp outside the 5-minute window`
- Missing fm_id → 401 `missing fm_id`
- Unknown fm_id → 401 `unknown fm_id`
- Unsigned POST → 401
- Duplicate handle → 400 `handle taken`; bad public_key → 400; reserved handle (`admin`) → 400
- GIF sha256 mismatch → 401; non-GIF bytes → 400 `not a gif -- magic bytes do not match`; truncated MP4 (ftyp, no moov) → 400 `corrupt or truncated video file`

## Findings

### NEW P2: `GET /api/platform-key` omits the `ok:true` envelope
**Repro:**
```
curl -s http://127.0.0.1:8473/api/platform-key
→ {"ephemeral":true,"key_id":"musefm-platform-v1","public_key":"seeuCeX7xA2Mcus6DFrOdr2c6v-vUufr5ViAPFZmmxQ"}
```
**Expected:** per /api/docs §2, successful responses are `{"ok": true, …}`.
Every other attestation endpoint (passport, signal credential, signed
activity, assert-identity-pubkey) returns the envelope; platform-key does
not. Cosmetic only — machine clients keying off `"ok"` to detect success
would misread it. No evidence of this in prior bug/agent files (deduped);
suggest marking P2 and either adding `"ok": true` to the handler or carving
an exception in the docs.

### Known observations (already on record, not re-reported)
- Local `/api/platform-key` reports `"ephemeral": true` (MUSEFM_PLATFORM_KEY
  unset): platform-signed attestations from this instance stop verifying
  after a restart. On production the key must be stable (reported
  bugs-2026-09-20-0046 as a prod-config observation).
- `/api/docs` family-services bar still lists MuseFM Arena and Exchange Pro
  (stale brand surface; arena was shut down 2026-09-21). Left for the parent
  loop — not the agent API surface I was asked to test.
