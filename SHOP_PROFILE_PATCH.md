# Profile spendable-balance integration (staged for the design-polish pass)

`Database.public_profile()` now includes `"spent"` and `"spendable"` keys
(db.py, template-agnostic — no template changes were made here to avoid
conflicting with the polish worker's edits to templates/profile.html).

Suggested profile.html addition (Aero-glass pill, next to the Signal/tier
row), e.g. in the stats block that renders `profile.signal`:

```html
{% if profile.spendable is defined %}
<span class="pill spendable-pill" title="Spendable Signal — gross earned minus shop spending. Lifetime Signal never decreases.">
  ⚡{{ profile.spendable }} spendable
</span>
{% endif %}
```

Copy guidance: "Spendable Signal" with a tooltip/link to /shop:
"Spending never lowers your lifetime Signal, tier, or pet growth."

Context for the copy: the shop spends from spendable = gross earned −
gross spent; lifetime Signal, tiers, Tidepal stages, and achievements all use
gross lifetime only. Full rulebook: GET /api/rewards/rules → tidepals.shop.
