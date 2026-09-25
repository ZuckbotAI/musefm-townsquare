/* MuseFM — signals. Every reaction widget is one compact rollout button
   (2026-09-25, Anthony): collapsed it shows the viewer's own signal (or the
   bolt) plus the total; opening it rolls the five signals out. Native
   <details> disclosure, so it works with zero JS — every pill is a plain
   form POST to /signals/react, and this script upgrades taps to JSON fetch
   and re-renders in place. Tapping a pill never closes the tray. */
(function () {
  'use strict';
  var META = {
    lit:   { emoji: '\u26A1', label: 'Lit' },
    idea:  { emoji: '\uD83D\uDCA1', label: 'Idea' },
    kind:  { emoji: '\uD83D\uDC9C', label: 'Kind' },
    fire:  { emoji: '\uD83D\uDD25', label: 'Fire' },
    build: { emoji: '\uD83D\uDE80', label: 'Build' }
  };
  var ORDER = ['lit', 'idea', 'kind', 'fire', 'build'];

  function closeOthers(except) {
    document.querySelectorAll('details.rxn-rollout[open]')
      .forEach(function (el) {
        if (el !== except) el.open = false;
      });
  }

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function renderWidget(w, d) {
    w.setAttribute('data-mine', d.mine || '');
    ORDER.forEach(function (key) {
      var btn = w.querySelector('.sig[data-reaction="' + key + '"]');
      if (!btn) return;
      var c = (d.counts && d.counts[key]) || 0;
      btn.querySelector('.sig-count').textContent = c;
      btn.classList.toggle('active', d.mine === key);
      btn.setAttribute('aria-pressed', d.mine === key ? 'true' : 'false');
      btn.setAttribute('aria-label', META[key].label + ' — ' + c + ' so far');
    });
    // rollout button: viewer's own signal (or the bolt) + the new total
    var summary = w.querySelector('summary.sig-rollout');
    if (summary) {
      var emojiEl = summary.querySelector('.sig-emoji');
      var countEl = summary.querySelector('.sig-count');
      if (emojiEl) emojiEl.textContent = (d.mine && META[d.mine]) ? META[d.mine].emoji : '\u26A1';
      if (countEl) countEl.textContent = d.total;
      summary.setAttribute('aria-label', 'Signals, ' + d.total +
        ' total — open to send a signal');
    }
  }

  function toast(msg) {
    if (window.fmToast) { window.fmToast(msg); return; }
    if (window.console) console.log('[signals] ' + msg);
  }

  function sendSignal(w, reaction) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    var body = {
      target_type: w.getAttribute('data-target-type'),
      target_id: parseInt(w.getAttribute('data-target-id'), 10),
      reaction: reaction,
      next: w.getAttribute('data-next') || '/',
      csrf_token: meta ? meta.content : ''
    };
    fetch('/signals/react', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json().then(function (d) { return {status: r.status, body: d}; }); }).then(function (res) {
      var d = res.body;
      if (!d.ok) {
        if (d.signin_url) { window.location.href = d.signin_url; return; }
        toast(d.error || 'signal failed');
        return;
      }
      renderWidget(w, d);
    }).catch(function () { toast('network hiccup — try again'); });
  }

  function wireWidget(w) {
    if (w._sigWired) return;
    w._sigWired = true;
    // one tap on a pill = the whole interaction. No long-press, no hover menu.
    w.querySelectorAll('.sig').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        sendSignal(w, btn.getAttribute('data-reaction'));
      });
    });
    // opening one tray closes the others; keep aria-expanded honest
    w.addEventListener('toggle', function () {
      var summary = w.querySelector('summary.sig-rollout');
      if (summary) summary.setAttribute('aria-expanded', w.open ? 'true' : 'false');
      if (w.open) closeOthers(w);
    });
  }
  document.querySelectorAll('.rxn').forEach(wireWidget);
  // Exposed so infinite-scroll feeds can wire signal widgets on
  // dynamically inserted cards.
  window.wireRxnWidget = wireWidget;

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.rxn')) closeOthers(null);
  });
})();
