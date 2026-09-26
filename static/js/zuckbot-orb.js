/* =========================================================================
 * zuckbot-orb.js — the stylized Zuckbot orb beside post composers
 * (2026-09-26, Anthony).
 *
 * Each `.zuckbot-orb` sits next to its form's textarea. While the user is
 * actively typing, the orb gets `.is-listening` and plays a subtle bob /
 * glow pulse. The class drops ~700ms after the last keystroke and
 * immediately on blur, so the orb settles without disturbing typing.
 * Transforms animate only the inner element — no layout shift.
 * prefers-reduced-motion: no animation at all.
 * ========================================================================= */
(function () {
  'use strict';

  var reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced) return;

  var SETTLE_MS = 700;

  function bind(orb) {
    var form = orb.closest('form');
    var ta = form ? form.querySelector('textarea') : null;
    if (!ta) return;
    var timer = null;

    function settle() {
      timer = null;
      orb.classList.remove('is-listening');
    }

    ta.addEventListener('input', function () {
      orb.classList.add('is-listening');
      if (timer) clearTimeout(timer);
      timer = setTimeout(settle, SETTLE_MS);
    });

    ta.addEventListener('blur', function () {
      if (timer) { clearTimeout(timer); timer = null; }
      orb.classList.remove('is-listening');
    });
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
