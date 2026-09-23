/* Muse FM — signals. Five one-tap signal pills, always visible, no picker.
   Every pill is a plain form POST to /signals/react (no-JS fallback); this
   script upgrades taps to JSON fetch and re-renders in place. Tapping the
   total toggles the breakdown. */
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

  function closeAll(except) {
    document.querySelectorAll('.rxn .sig-breakdown:not([hidden])')
      .forEach(function (el) {
        if (el !== except) el.hidden = true;
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
    var totalBtn = w.querySelector('.sig-total');
    totalBtn.textContent = d.total;
    totalBtn.setAttribute('aria-label', d.total + ' total signals — see breakdown');
    var bd = w.querySelector('.sig-breakdown');
    var bdHtml = '';
    ORDER.forEach(function (key) {
      if (d.counts && d.counts[key]) {
        bdHtml += '<div class="sig-brow" data-reaction="' + key + '"><span>' +
          META[key].emoji + ' ' + META[key].label + '</span><b>' + d.counts[key] + '</b></div>';
      }
    });
    if (!bdHtml) bdHtml = '<div class="sig-brow sig-brow-empty"><span>No signals yet — be the first.</span></div>';
    bd.innerHTML = bdHtml;
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
      closeAll();
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
    var totalBtn = w.querySelector('.sig-total');
    if (totalBtn) {
      totalBtn.addEventListener('click', function () {
        var bd = w.querySelector('.sig-breakdown');
        var willOpen = bd.hidden;
        closeAll();
        bd.hidden = !willOpen;
      });
    }
  }
  document.querySelectorAll('.rxn').forEach(wireWidget);
  // Exposed so infinite-scroll feeds can wire signal widgets on
  // dynamically inserted cards.
  window.wireRxnWidget = wireWidget;

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.rxn')) closeAll();
  });
  document.addEventListener('scroll', function () { closeAll(); }, { passive: true });
})();
