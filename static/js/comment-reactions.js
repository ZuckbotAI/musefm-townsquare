/* MuseFM — comment fan-out reactions (2026-09-24).
   The smiley trigger opens a fan-out picker with the 8 allowlist emoji
   (mirrors REACT_EMOJIS in db.py). Every emoji/chip is a real form POST to
   /comment/react (no-JS fallback); this script upgrades taps to JSON fetch
   and re-renders counts in place. Tapping your own reaction removes it
   (FB-style toggle). */
(function () {
  'use strict';

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function closeAll(except) {
    document.querySelectorAll('.crxn-picker:not([hidden])').forEach(function (p) {
      if (p !== except) {
        p.hidden = true;
        var t = p.closest('.crxn').querySelector('.crxn-trigger');
        if (t) t.setAttribute('aria-expanded', 'false');
      }
    });
  }

  function mineSet(w) {
    var out = {};
    w.querySelectorAll('.crxn-emoji.is-mine, .crxn-chip.is-mine').forEach(function (b) {
      out[b.getAttribute('data-emoji')] = true;
    });
    return out;
  }

  // Rebuild the chips row from a {emoji: count} map, keeping the viewer's
  // own reactions marked. Chips are real forms (no-JS fallback preserved).
  function renderChips(w, counts) {
    var chips = w.querySelector('.crxn-chips');
    if (!chips) return;
    var mine = mineSet(w);
    var ttype = w.getAttribute('data-target-type');
    var tid = w.getAttribute('data-target-id');
    var csrf = w.getAttribute('data-csrf') || '';
    var next = w.getAttribute('data-next') || '';
    var keys = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
    var html = '';
    keys.forEach(function (e) {
      var isMine = !!mine[e];
      html += '<form method="post" action="/comment/react" class="crxn-form">' +
        '<input type="hidden" name="csrf_token" value="' + esc(csrf) + '">' +
        '<input type="hidden" name="target_type" value="' + esc(ttype) + '">' +
        '<input type="hidden" name="target_id" value="' + esc(tid) + '">' +
        '<input type="hidden" name="emoji" value="' + esc(e) + '">' +
        '<input type="hidden" name="action" value="remove">' +
        '<input type="hidden" name="next" value="' + esc(next) + '">' +
        '<button type="submit" class="crxn-chip' + (isMine ? ' is-mine' : '') + '" data-emoji="' + esc(e) + '"' +
        ' aria-label="' + esc(e) + ': ' + counts[e] + ' reaction' + (counts[e] === 1 ? '' : 's') +
        (isMine ? ' (yours — tap to remove)' : '') + '"' +
        ' title="' + (isMine ? 'Remove your reaction' : esc(e)) + '">' +
        esc(e) + '<b>' + counts[e] + '</b></button></form>';
    });
    chips.innerHTML = html;
  }

  function markMine(w, emoji, isMine) {
    w.querySelectorAll('[data-emoji="' + emoji + '"]').forEach(function (b) {
      b.classList.toggle('is-mine', !!isMine);
      b.setAttribute('aria-pressed', isMine ? 'true' : 'false');
    });
  }

  function send(w, emoji, remove, btn) {
    var payload = {
      csrf_token: w.getAttribute('data-csrf') || '',
      target_type: w.getAttribute('data-target-type'),
      target_id: parseInt(w.getAttribute('data-target-id'), 10),
      emoji: emoji,
      next: w.getAttribute('data-next') || ''
    };
    if (remove) payload.action = 'remove';
    if (btn) btn.disabled = true;
    return fetch('/comment/react', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, body: d }; });
    }).then(function (res) {
      if (btn) btn.disabled = false;
      var d = res.body || {};
      if (res.status === 401 && d.signin_url) { window.location.href = d.signin_url; return; }
      if (!d.ok) {
        if (window.toast) window.toast(d.error || 'reaction failed');
        return;
      }
      // Track the viewer's own reaction locally (server returns only the
      // emoji just acted on, not the full set).
      markMine(w, emoji, !remove);
      renderChips(w, d.reactions || {});
      var picker = w.querySelector('.crxn-picker');
      if (picker && !remove) { picker.hidden = true; }
      var t = w.querySelector('.crxn-trigger');
      if (t) t.setAttribute('aria-expanded', 'false');
    }).catch(function () {
      if (btn) btn.disabled = false;
      if (window.toast) window.toast('reaction failed — try again');
    });
  }

  // Trigger: fan the picker open/closed.
  document.addEventListener('click', function (ev) {
    var trigger = ev.target.closest('.crxn-trigger');
    if (trigger) {
      var w = trigger.closest('.crxn');
      var picker = w.querySelector('.crxn-picker');
      var open = picker.hidden;
      closeAll(picker);
      picker.hidden = !open;
      trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
      ev.preventDefault();
      return;
    }
    // Emoji in the picker, or a chip: FB-style — tap your own to remove,
    // tap anything else to add.
    var btn = ev.target.closest('.crxn-emoji, .crxn-chip');
    if (btn) {
      var w2 = btn.closest('.crxn');
      var emoji = btn.getAttribute('data-emoji');
      var remove = btn.classList.contains('is-mine');
      ev.preventDefault();
      send(w2, emoji, remove, btn);
      return;
    }
    // Click anywhere else closes open pickers.
    if (!ev.target.closest('.crxn')) closeAll(null);
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') closeAll(null);
  });

  // Let no-JS form submits still work when JS handled nothing (progressive
  // enhancement guard): the delegated click above calls preventDefault, so
  // forms only ever submit natively if JS is off or errors before wiring.
})();
