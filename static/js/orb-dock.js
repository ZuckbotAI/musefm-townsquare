/* =========================================================================
 * orb-dock.js — the Muse orb's three positions (2026-09-23 spec).
 *
 *  - HERO: on the homepage, when the hero card's orb stage is in view, the
 *    orb snaps into the stage. Click/tap it for a saying.
 *  - FLOAT: scrolled down past the hero, the orb snaps out and floats,
 *    fixed at the viewport's right edge above the miniplayer — it stays
 *    visible as you scroll.
 *  - HOME: on pages with no hero stage, the orb parks next to the home
 *    "Muse FM" logo button in the top bar.
 *
 * No placeholder outlines anywhere: the home anchor and the hero stage are
 * invisible layout boxes only. Drag-to-place is retired (the orb is
 * tap-only); double-click re-syncs the orb to the state machine.
 * prefers-reduced-motion: instant placement, no flying.
 * The wrap is a direct child of <body> and moves via transform only, so it
 * never disturbs page layout.
 * ========================================================================= */
(function () {
  'use strict';

  var ORB = 96; // must match ORB_SIZE in muse-orb.js
  var FLOAT_SCALE = 0.75;   // floating orb size
  var HOME_SCALE = 0.52;    // parked by the logo button (60px topbar)
  var FLOAT_BOTTOM = 132;   // float spot: above the miniplayer
  var FLOAT_RIGHT = 16;

  function $(sel) { return document.querySelector(sel); }

  var reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var wrap = null;
  var lastKey = null; // last placed target, to avoid redundant transforms

  function stage() { return $('#hero-orb-stage'); }
  function home() { return $('#orb-home'); }

  function stageInView() {
    var st = stage();
    if (!st) return false;
    var r = st.getBoundingClientRect();
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var mid = r.top + r.height / 2;
    return mid > vh * 0.22 && mid < vh * 0.82;
  }

  // where the orb belongs right now: {cx, cy, scale} in viewport coords
  function target() {
    var st = stage();
    if (st) {
      if (stageInView()) {
        // HERO: snap into the hero card stage for click-for-sayings
        var r = st.getBoundingClientRect();
        var s = Math.min(1.5, (r.width / ORB) * 0.85);
        return { cx: r.left + r.width / 2, cy: r.top + r.height / 2, s: s, where: 'hero' };
      }
      // FLOAT: hero scrolled away — stay visible, fixed at the right edge
      var vw = window.innerWidth, vh = window.innerHeight;
      var d = ORB * FLOAT_SCALE;
      return {
        cx: vw - FLOAT_RIGHT - d / 2,
        cy: vh - FLOAT_BOTTOM - d / 2,
        s: FLOAT_SCALE, where: 'float'
      };
    }
    // HOME: no hero on this page — park by the home logo button
    var h = home();
    if (h) {
      var hr = h.getBoundingClientRect();
      return { cx: hr.left + hr.width / 2, cy: hr.top + hr.height / 2, s: HOME_SCALE, where: 'home' };
    }
    return { cx: window.innerWidth - 60, cy: 40, s: HOME_SCALE, where: 'home' };
  }

  function place(instant) {
    if (!wrap) return;
    var t = target();
    var x = Math.round(t.cx - (ORB * t.s) / 2);
    var y = Math.round(t.cy - (ORB * t.s) / 2);
    var key = t.where + ':' + x + ',' + y + ',' + t.s;
    if (key === lastKey && !instant) return;
    lastKey = key;
    wrap.style.transform = 'translate(' + x + 'px,' + y + 'px) scale(' + t.s + ')';
    wrap.setAttribute('data-orb-where', t.where);
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

  // The wrap must be a direct child of <body>: the core orb can re-insert
  // it into the topbar flow (double-click send-home), and only <body>
  // keeps the fixed-transform positioning predictable.
  function ensureOnBody() {
    if (wrap && wrap.parentNode !== document.body) document.body.appendChild(wrap);
  }

  function takeOver() {
    if (!wrap) return;
    ensureOnBody();
    wrap.classList.add('muse-orb-dockmanaged');
    wrap.style.transformOrigin = 'top left';
    if (reduced) wrap.style.transition = 'none'; // no flying, instant snaps
    // double-click = the orb's own "send home" -> re-sync to the state machine
    // (the core re-inserts the wrap beside its anchor on send-home, so pull
    // it back out to <body> here too).
    wrap.addEventListener('dblclick', function () {
      ensureOnBody();
      lastKey = null;
      setTimeout(function () { place(true); }, 60);
    });
    place(true);
    wrap.style.visibility = 'visible';
  }

  function boot() {
    wrap = $('.muse-orb-wrap');
    if (!wrap) { setTimeout(boot, 250); return; }
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
