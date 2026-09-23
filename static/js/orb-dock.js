/* =========================================================================
 * orb-dock.js — the Muse orb's deliberate home.
 *
 * The orb lives in a fixed dock chip at the top of every page (no more
 * awkward 96px orb wedged into the topbar flow). On the homepage, when the
 * "Zuckbot Says" section scrolls into view, the orb pops out of the dock
 * and flies into the section's orb stage, beside the rotating quotes; when
 * the section scrolls away, it flies home to the dock.
 *
 * Rules:
 *  - If the user drags the orb (its built-in easter egg), management yields
 *    immediately — the user's placement wins. Double-click (orb's "send
 *    home") hands management back.
 *  - prefers-reduced-motion: no flying; the orb stays in its dock.
 *  - No layout impact: the wrap is position:fixed, moved via transform only.
 * ========================================================================= */
(function () {
  'use strict';

  var ORB = 96; // must match ORB_SIZE in muse-orb.js

  function $(sel) { return document.querySelector(sel); }

  var dock = $('#orb-dock');
  if (!dock) return;

  var reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var wrap = null;
  var managed = true;   // false once the user grabs the orb themselves
  var lastKey = null;   // last placed target, to avoid redundant transforms

  function stage() { return $('#zuckbot-orb-stage'); }

  function dockScale() {
    // dock is 104px desktop / 76px mobile; fit the 96px orb inside with margin
    var box = Math.min(dock.clientWidth || 104, dock.clientHeight || 104);
    return Math.min(1, (box - 8) / ORB);
  }

  function stageInView() {
    var st = stage();
    if (!st) return false;
    var r = st.getBoundingClientRect();
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var mid = r.top + r.height / 2;
    return mid > vh * 0.22 && mid < vh * 0.82;
  }

  // viewport coords -> fixed-position transform for the wrap (origin: top left)
  function transformFor(cx, cy, scale) {
    var x = Math.round(cx - (ORB * scale) / 2);
    var y = Math.round(cy - (ORB * scale) / 2);
    return { x: x, y: y, s: scale, css: 'translate(' + x + 'px,' + y + 'px) scale(' + scale + ')' };
  }

  function place(instant) {
    if (!wrap || !managed) return;
    var st = stage();
    var useStage = st && !reduced && stageInView();
    var el = useStage ? st : dock;
    var r = el.getBoundingClientRect();
    var scale = useStage ? 1 : dockScale();
    var t = transformFor(r.left + r.width / 2, r.top + r.height / 2, scale);
    var key = t.x + ',' + t.y + ',' + t.s;
    if (key === lastKey && !instant) return;
    lastKey = key;
    wrap.style.transform = t.css;
    if (st) st.classList.toggle('orb-here', !!useStage);
    dock.classList.toggle('orb-away', !!useStage);
  }

  var ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    (window.requestAnimationFrame || setTimeout)(function () {
      ticking = false;
      place(false);
    }, 16);
  }

  function takeOver() {
    if (!wrap) return;
    wrap.classList.add('muse-orb-dockmanaged');
    wrap.style.transformOrigin = 'top left';
    // user grabbed it themselves -> yield completely: drop our positioning
    // so the orb's native drag (left/top) works, and stop managing
    var fx = wrap.querySelector('canvas');
    if (fx) {
      fx.addEventListener('pointerdown', function () {
        managed = false;
        wrap.classList.remove('muse-orb-dockmanaged');
        wrap.style.transform = '';
      }, { capture: true });
    }
    // double-click = orb's own "send home" -> resume management
    wrap.addEventListener('dblclick', function () {
      managed = true;
      wrap.classList.add('muse-orb-dockmanaged');
      wrap.style.transformOrigin = 'top left';
      lastKey = null;
      setTimeout(function () { place(true); }, 60);
    });
    place(true);
    wrap.style.visibility = 'visible';
  }

  function boot() {
    wrap = $('.muse-orb-wrap');
    if (!wrap) { setTimeout(boot, 250); return; }
    // a saved drag position means the user placed it before -> hands off
    var saved = null;
    try { saved = localStorage.getItem('muse-orb-pos-v1'); } catch (e) {}
    if (saved) { managed = false; return; }
    wrap.style.visibility = 'hidden'; // avoid one-frame flash in the topbar flow
    takeOver();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
