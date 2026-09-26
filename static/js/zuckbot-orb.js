/* =========================================================================
 * zuckbot-orb.js — the stylized Zuckbot orb beside post composers
 * (2026-09-26, Anthony; pass B: the orb is alive).
 *
 * Each `.zuckbot-orb` sits next to its form's textarea.
 * - Idle: CSS breathes on its own (zb-breathe).
 * - While typing: `.is-listening` plays the bob + glow pulse.
 * - Each burst of keystrokes re-triggers a `.zb-hit` ripple ring
 *   (throttled to one per 140ms so fast typing doesn't strobe).
 * - `--zb-charge` (0..1) ramps the aura/core glow with input length,
 *   so the orb brightens as the note fills up.
 * - A live character counter (`.zb-count`) is injected into the
 *   composer's row and warns as the limit approaches.
 * The class drops ~700ms after the last keystroke and immediately on
 * blur, so the orb settles without disturbing typing.
 * Transforms animate only inner elements — no layout shift.
 * prefers-reduced-motion: no JS-driven motion at all.
 * ========================================================================= */
(function () {
  'use strict';

  var reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced) return;

  var SETTLE_MS = 700;
  var RIPPLE_MS = 140;

  function bind(orb) {
    var form = orb.closest('form');
    var ta = form ? form.querySelector('textarea') : null;
    if (!ta) return;
    var timer = null;
    var lastRipple = 0;

    // live character counter in the composer row
    var row = form.querySelector('.wall-composer-row');
    var count = null;
    if (row && !row.querySelector('.zb-count')) {
      count = document.createElement('span');
      count.className = 'zb-count';
      count.setAttribute('aria-live', 'polite');
      row.insertBefore(count, row.firstChild);
    } else if (row) {
      count = row.querySelector('.zb-count');
    }
    var max = ta.maxLength > 0 ? ta.maxLength : 280;

    function paint() {
      var len = ta.value.length;
      var charge = Math.max(0, Math.min(1, len / max));
      orb.style.setProperty('--zb-charge', charge.toFixed(3));
      if (count) {
        var left = max - len;
        count.textContent = left + ' left';
        count.classList.toggle('zb-low', left <= 40 && left > 0);
        count.classList.toggle('zb-empty', left <= 0);
      }
    }

    function ripple() {
      var now = Date.now();
      if (now - lastRipple < RIPPLE_MS) return;
      lastRipple = now;
      orb.classList.remove('zb-hit');
      void orb.offsetWidth; /* restart the animation */
      orb.classList.add('zb-hit');
    }

    function settle() {
      timer = null;
      orb.classList.remove('is-listening');
    }

    ta.addEventListener('input', function () {
      orb.classList.add('is-listening');
      ripple();
      paint();
      if (timer) clearTimeout(timer);
      timer = setTimeout(settle, SETTLE_MS);
    });

    ta.addEventListener('focus', paint);

    ta.addEventListener('blur', function () {
      if (timer) { clearTimeout(timer); timer = null; }
      orb.classList.remove('is-listening');
    });

    paint();
  }

  function init() {
    var orbs = document.querySelectorAll('.zuckbot-orb');
    for (var i = 0; i < orbs.length; i++) bind(orbs[i]);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
