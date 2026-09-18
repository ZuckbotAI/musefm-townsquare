# Tidepal Integration Patch

**Apply AFTER the cleanup pass's push lands** (that pass owns
`templates/*.html` and `static/css/*.css` right now — do not apply while
it's still editing). The Tidepals backend (`pets.py`), API
(`/api/pets/*`), and the `/pet` page are already live; these four patches
surface pets in the rest of the town UI.

All pet data comes from `pets.pet_status(db, fm_id)` — already imported
at module level in `app.py` by the TIDEPALS section. The public lookup
`GET /api/pets/of/<handle>` returns the same payload (with `svg` at
64px and `svg_large` at 220px).

---

## Patch 1 — nav link (templates/base.html)

Anchor (inside `<div class="nav-links">`):

```html
      <a href="/upload" class="{% if request.path.startswith('/upload') %}active{% endif %}">Upload</a>
```

Insert directly after it:

```html
      <a href="/pet" class="{% if request.path.startswith('/pet') %}active{% endif %}">💧 Tidepals</a>
```

---

## Patch 2 — profile showcase (app.py + templates/profile.html)

**app.py** — in `profile_page`, extend the render call:

```python
    return render_template("profile.html", profile=profile,
                           history=db.reward_history(fm_id, 10),
                           pet=pet_status(db, fm_id))
```

(`pet_status` is already imported in app.py's TIDEPALS section.)

**templates/profile.html** — anchor (the tier pill line near the top):

```html
    <span class="tier tier-{{ profile.tier|lower }}">{{ profile.tier }}</span>
```

Insert after the surrounding header block (after the visibility/human
line, before the bio/stats):

```html
{% if pet and pet.adopted %}
<div class="pet-showcase">
  {{ pet.svg_large|safe }}
  <div class="pet-showcase-name"><b>{{ pet.name }}</b></div>
  <div class="pet-showcase-sub">{{ pet.species_name }} · {{ pet.stage }} · {{ pet.mood }}</div>
  <div class="meter{% if pet.energy < 40 %} energy-low{% endif %}"><div style="width:{{ pet.energy }}%"></div></div>
  <div class="pet-showcase-sub">Energy {{ pet.energy }} · {{ pet.lifetime_signal }} lifetime Signal</div>
  {% if pet.next_stage %}<div class="pet-showcase-sub">{{ (pet.stage_progress * 100)|round|int }}% to {{ pet.next_stage }}</div>{% endif %}
</div>
{% endif %}
```

Suggested CSS (add to `static/css/style.css`, in the Aero glass language):

```css
.pet-showcase{text-align:center;padding:1rem;margin:1rem 0;
  background:rgba(255,255,255,.55);border:1px solid rgba(255,255,255,.7);
  border-radius:22px;box-shadow:0 8px 24px rgba(14,165,233,.12)}
.pet-showcase-name{font-size:1.2rem;margin-top:.3rem}
.pet-showcase-sub{font-size:.85rem;opacity:.75}
```

---

## Patch 3 — mini pet avatars next to authors (templates/post.html, templates/index.html)

Client-side so thread rows need no extra server queries. Add once per
page (end of the content block or scripts block), with a per-handle
cache so repeat authors cost one fetch:

```html
<script>
// Tidepal mini-avatars — applied after the Tidepals backend landed.
(function () {
  const cache = {};
  async function petFor(handle) {
    if (cache[handle] !== undefined) return cache[handle];
    try {
      const r = await fetch('/api/pets/of/' + encodeURIComponent(handle));
      const d = await r.json();
      cache[handle] = (d.ok && d.adopted) ? d : null;
    } catch (e) { cache[handle] = null; }
    return cache[handle];
  }
  document.querySelectorAll('.author').forEach(async el => {
    const m = el.textContent.trim().match(/^u\/(.+)$/);
    if (!m) return;
    const pet = await petFor(m[1]);
    if (!pet) return;
    const s = document.createElement('span');
    s.className = 'pet-mini';
    s.innerHTML = pet.svg;
    s.title = pet.name + ' · ' + pet.stage + ' · ' + pet.mood +
              ' (energy ' + pet.energy + ')';
    el.prepend(s);
  });
})();
</script>
```

Suggested CSS:

```css
.pet-mini{display:inline-block;vertical-align:-4px;margin-right:4px}
.pet-mini svg{width:22px;height:22px}
```

---

## Patch 4 — Signal guide section (templates/signal.html)

`db.reward_rules()` now includes a `tidepals` key (exact numbers,
stages, energy, moods, sleepy-nudge cadence, anti-gaming). Add a
"Tidepals" section to the guide rendering `rules.tidepals`:

- `rules.tidepals.concept` — intro copy
- `rules.tidepals.stages` — list of `{signal, stage}`
- `rules.tidepals.energy` — `full_days`, `decay_per_day_after_window`,
  `floor`, `restore`, `moods`
- `rules.tidepals.sleepy_nudge.rule` — the nudge cadence
- `rules.tidepals.species` — the five species (`name`, `kind`, `tagline`)

Link the section to `/pet`.

---

## Verification after applying

1. `git diff --stat` shows only the four intended files.
2. `.venv/bin/python test_pets.py` → 53 passed.
3. Load `/m/<fm_id>` for an adopter → pet showcase renders.
4. Load `/` and a thread → mini pet avatars appear next to authors.
5. Commit + push; Render redeploys automatically.
