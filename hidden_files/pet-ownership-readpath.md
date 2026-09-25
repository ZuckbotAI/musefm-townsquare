# Pet ownership: canonical read path (blocker #2, 2026-09-23)

Decision: **tidepals is the single source of truth for pet state; the
village client reads the companion from the existing pet endpoints, NOT
from the player snapshot.** No second source of truth is created.

## The three stores and what each one answers

| Store | Table(s) | Answers | Read via |
|---|---|---|---|
| tidepals (pets.py) | `tidepals` | "What IS my pet?" — species, name, stage, energy, mood, trait, wardrobe, pond status | `GET /api/pets/of/<handle>` (public; the client passes its OWN handle — this is the browser-usable path). `GET /api/pets/mine` exposes the same data but requires signed agent auth (action `pets_mine`), so the canvas client does NOT use it. |
| row_pet_claims (row.py) | `row_pet_claims` | "Who owns the name X?" — name -> owning fm_id, used ONLY by snapshot validation | internal to `row.save_player`; never read for pet state |
| row_player.petOwners | inside snapshot JSON | an opaque validated echo of ownership claims the client sent | `GET /api/row/player` returns it, but clients must NEVER treat it as pet state |

bond.py's `bond_memory` is a fourth store (attachment history) and is
deliberately outside this unification — out of scope per brief.

## Why not embed the pet in GET /api/row/player?

The brief explicitly permits derived embedding, so a read-time join
would not itself create a second source. It was still rejected: the
player snapshot is an opaque, client-written blob with server-side
shape validation, and embedding pet body fields (name, species, stage)
into it would fork pet state into a cache that can only drift — the
tidepals row keeps changing (energy, stage, mood, renames) while the
snapshot only changes when the client saves. The pet endpoints already
expose the canonical state (`/api/pets/of/<handle>` with the session's
own handle), so the client renders the companion straight from the
source of truth. No second read path is needed, and the snapshot keeps
carrying only `petOwners` (the ownership echo the contract specifies).

## Sync contract (write side)

- `pets.adopt()` success -> upsert `row_pet_claims(name -> fm_id)`
- `pets.rename_pet()` -> release old name's claim (only if it points at
  the renamer), upsert new name
- `pets.pond_adopt()` -> upsert claim to the new keeper (real transfer)
- `pets.release_pet()` -> claim row intentionally lingers; pond pets are
  excluded from ownership checks, and the next real adoption's upsert
  transfers the name
- `row.save_player()` -> for each petOwners claim, checks tidepals FIRST
  (owned rows: in_pond=0, non-pond key): if the name is owned and the
  claimant isn't among its owners, the claim is dropped and reported in
  `droppedClaims` — never transferred. Then the registry rules apply.
- `row.backfill_pet_claims()` -> idempotent, INSERT OR IGNORE seed from
  tidepals; runs at app startup (init_db). Never overwrites/deletes.

## Known edges (accepted for launch)

1. **Duplicate pet names.** tidepals does not enforce global name
   uniqueness, so two keepers can own pets with the same name. The
   registry holds one owner per name (last real adoption wins). The
   snapshot check keeps a claim when the claimant is among the name's
   tidepals owners, so true owners aren't locked out; the registry may
   still attribute the name to only one of them. Enforcing uniqueness
   in adopt() is a product call for Anthony, not a launch fix.
2. **Released (pond) pets keep their claim rows.** A pond pet belongs to
   nobody; its old claim can't be squatted via snapshot (pond rows are
   excluded from the ownership check and the stale claim still names
   the old owner), and pond_adopt upserts the name to the new keeper.
3. **Claim writes from pets.py are best-effort.** They run after the
   tidepals write commits and must never break adoption/rename UX; the
   backfill and the snapshot-time tidepals check close any gap.
