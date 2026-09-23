/* Muse FM — signals. Five one-tap signal pills, always visible, no picker
   (on phones, shorts collapse them into one hub button that unfurls a
   wheel). Every pill is a plain form POST to /signals/react (no-JS
   fallback); this script upgrades taps to JSON fetch and re-renders in
   place. Tapping the total toggles the breakdown (or the wheel on mobile
   shorts). */
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

  // shorts wheel hub shows the LATEST reaction's emoji (Anthony 2026-09-23).
  // The backend summary has no "latest" field and we can't touch it, so the
  // latest is tracked client-side: the reaction just submitted through this
  // widget IS the latest at that moment. Persisted per target in
  // localStorage so it survives reloads; cleared when the viewer removes
  // their reaction (we no longer know the true latest).
  var LATEST_KEY_PREFIX = 'musefm-latest-signal:v1:';

  function latestKey(w) {
    return LATEST_KEY_PREFIX + w.getAttribute('data-target-type') + ':' +
      w.getAttribute('data-target-id');
  }

  function getLatest(w) {
    try {
      var v = window.localStorage.getItem(latestKey(w));
      return (v && META[v]) ? v : null;
    } catch (e) { return null; }
  }

  function setLatest(w, reaction) {
    try {
      if (reaction && META[reaction]) {
        window.localStorage.setItem(latestKey(w), reaction);
      } else {
        window.localStorage.removeItem(latestKey(w));
      }
    } catch (e) { /* private mode etc. — hub just falls back to the count */ }
  }

  // Wheel hub only: paint the latest reaction's emoji big, with the numeric
  // total demoted to a small badge. No latest known (or zero reactions) ->
  // keep the current look: the plain count.
  function paintWheelHub(w, total) {
    var totalBtn = w.querySelector('.sig-total');
    if (!totalBtn) return;
    var latest = getLatest(w);
    if (latest && total > 0) {
      totalBtn.innerHTML =
        '<span class="hub-latest" aria-hidden="true">' + META[latest].emoji + '</span>' +
        '<span class="hub-total">' + total + '</span>';
      totalBtn.setAttribute('aria-label', META[latest].label +
        ' \u2014 latest signal, ' + total + ' total \u2014 open reactions');
    } else {
      totalBtn.textContent = total;
      totalBtn.setAttribute('aria-label',
        total + ' total signals \u2014 open reactions');
    }
  }

  function closeAll(except) {
    document.querySelectorAll('.rxn .sig-breakdown:not([hidden])')
      .forEach(function (el) {
        if (el !== except) el.hidden = true;
      });
  }

  function closeWheels(except) {
    document.querySelectorAll('.rxn.open')
      .forEach(function (el) {
        if (el !== except) el.classList.remove('open');
      });
  }

  // shorts on phones: the signals collapse into one hub button that
  // unfurls a wheel; everywhere else they stay as visible pills.
  function isShortsWheel(w) {
    return !!(w.closest('.short-rxn') &&
      window.matchMedia('(max-width: 640px)').matches);
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
    if (isShortsWheel(w)) {
      paintWheelHub(w, d.total);
    } else {
      totalBtn.textContent = d.total;
      totalBtn.setAttribute('aria-label', d.total +
        ' total signals \u2014 see breakdown');
    }
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
      closeWheels();
      // the reaction just submitted is the latest — track it for the hub
      if (d.action === 'added' || d.action === 'switched') {
        setLatest(w, reaction);
      } else if (d.action === 'removed') {
        setLatest(w, null);
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
    var totalBtn = w.querySelector('.sig-total');
    if (totalBtn) {
      if (isShortsWheel(w)) {
        // page load: upgrade the server-rendered count to latest-emoji hub
        // when we have a persisted latest for this target
        var t = parseInt((totalBtn.textContent || '').replace(/\D/g, ''), 10) || 0;
        paintWheelHub(w, t);
      }
      totalBtn.addEventListener('click', function () {
        if (isShortsWheel(w)) {
          var willOpen = !w.classList.contains('open');
          closeAll();
          closeWheels();
          if (willOpen) w.classList.add('open');
          return;
        }
        var bd = w.querySelector('.sig-breakdown');
        var willOpenBd = bd.hidden;
        closeAll();
        bd.hidden = !willOpenBd;
      });
    }
  }
  document.querySelectorAll('.rxn').forEach(wireWidget);
  // Exposed so infinite-scroll feeds can wire signal widgets on
  // dynamically inserted cards.
  window.wireRxnWidget = wireWidget;

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.rxn')) { closeAll(); closeWheels(); }
  });
  document.addEventListener('scroll', function () { closeAll(); closeWheels(); }, { passive: true });
})();
