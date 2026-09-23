/* =========================================================================
 * orb-dock.js — the Muse orb's dock positions (2026-09-23 spec, floating
 * dock phase).
 *
 *  - HERO: on the homepage, when the hero card's orb stage is in view, the
 *    orb snaps into the stage. Click/tap it for a saying.
 *  - FLOAT: everywhere else — a fixed overlay that stays visible while the
 *    user scrolls. Home past the hero, Shorts, forum, every main surface.
 *    The user can drag it anywhere (touch + mouse, via the pointer
 *    handlers in muse-orb.js); the spot persists across reloads in
 *    localStorage. Double-click clears the saved spot and re-syncs to the
 *    smart default corner (bottom-right, above the miniplayer; smaller
 *    orb on mobile).
 *
 * The wrap is a direct child of <body> and moves via transform only, so it
 * never disturbs page layout. Its z-index sits below the miniplayer, the
 * mod bulk bar and other interactive chrome (style.css) — the orb never
 * covers media controls or the Shorts reaction rail.
 * prefers-reduced-motion: instant placement, no flying.
 * ========================================================================= */
(function () {
  'use strict';

  var ORB = 96; // must match ORB_SIZE in muse-orb.js
  var POS_KEY = 'musefm-orb-dock-pos-v1';
  var FLOAT_RIGHT = 16;
  var FLOAT_BOTTOM = 132; // default corner clears the miniplayer
  var MOBILE_BP = 640;

  function $(sel) { return document.querySelector(sel); }
  function isMobile() { return (window.innerWidth || 0) <= MOBILE_BP; }
  function floatScale() { return isMobile() ? 0.62 : 0.75; } // small on mobile

  var reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var wrap = null;
  var lastKey = null; // last placed transform, to avoid redundant writes
  var lastWhere = null; // last dock state
  var base = null; // un-offset float target {x, y, s} — drag math reads this
  var off = { dx: 0, dy: 0 }; // user offset from the default corner, px
  var savedPos = loadPos(); // persisted float spot {x, y} top-left px, or null

  function loadPos() {
    try {
      var p = JSON.parse(localStorage.getItem(POS_KEY) || 'null');
      if (p && isFinite(p.x) && isFinite(p.y)) return { x: +p.x, y: +p.y };
    } catch (e) { /* private mode */ }
    return null;
  }
  function storePos(x, y) {
    savedPos = { x: x, y: y };
    try {
      localStorage.setItem(POS_KEY, JSON.stringify({ x: Math.round(x), y: Math.round(y) }));
    } catch (e) { /* private mode */ }
  }
  function dropPos() {
    savedPos = null;
    try { localStorage.removeItem(POS_KEY); } catch (e) { /* private mode */ }
  }

  // keep a persisted spot inside the viewport (rotation, resize, new device)
  function clampFloat(x, y, s) {
    var d = ORB * s;
    var vw = window.innerWidth || 0, vh = window.innerHeight || 0;
    return {
      x: Math.max(0, Math.min(Math.max(0, vw - d), x)),
      y: Math.max(0, Math.min(Math.max(0, vh - d), y))
    };
  }

  function stage() { return document.getElementById('hero-orb-stage'); }

  function stageInView() {
    var st = stage();
    if (!st) return false;
    // top of the homepage: the orb starts in its hero bubble even when the
    // stage sits just below the fold on small screens (2026-09-23).
    if ((window.scrollY || window.pageYOffset || 0) < 80) return true;
    var r = st.getBoundingClientRect();
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var mid = r.top + r.height / 2;
    return mid > vh * 0.22 && mid < vh * 0.82;
  }

  // default float corner for this viewport: top-left px of the orb box
  function defaultFloat(s) {
    var vw = window.innerWidth || 0, vh = window.innerHeight || 0;
    var d = ORB * s;
    return {
      x: Math.max(0, vw - FLOAT_RIGHT - d),
      y: Math.max(0, vh - FLOAT_BOTTOM - d)
    };
  }

  // where the orb belongs right now: {x, y, s} top-left px + scale
  function target() {
    var st = stage();
    if (st && stageInView()) {
      // HERO: snap into the hero card stage for click-for-sayings
      var r = st.getBoundingClientRect();
      var s = Math.min(1.5, (r.width / ORB) * 0.85);
      return {
        x: r.left + (r.width - ORB * s) / 2,
        y: r.top + (r.height - ORB * s) / 2,
        s: s, where: 'hero'
      };
    }
    // FLOAT: fixed overlay on every page, visible while scrolling
    var fs = floatScale();
    var d = defaultFloat(fs);
    return { x: d.x, y: d.y, s: fs, where: 'float' };
  }

  function place(instant) {
    if (!wrap) return;
    // hands off while the user is mid-drag — muse-orb.js drives the transform
    if (wrap.classList.contains('muse-orb-dragging')) return;
    var t = target();
    if (t.where !== lastWhere) {
      if (t.where === 'float' && savedPos) {
        // restore the user's persisted spot for this viewport
        var c = clampFloat(savedPos.x, savedPos.y, t.s);
        off.dx = c.x - t.x;
        off.dy = c.y - t.y;
      } else {
        // hero snap, or a fresh float with no saved spot: no offset
        off.dx = 0; off.dy = 0;
      }
      lastWhere = t.where;
    }
    base = { x: t.x, y: t.y, s: t.s };
    var x = Math.round(t.x + off.dx);
    var y = Math.round(t.y + off.dy);
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
    // rAF can stall when the page isn't painting (background tab, headless
    // renderers) — the setTimeout fallback keeps the orb tracking the stage.
    // (2026-09-23: headless WebKit fired rAF once, then never again, freezing
    // the orb after the first scroll.)
    var done = false;
    function run() {
      if (done) return;
      done = true;
      ticking = false;
      place(false);
    }
    if (window.requestAnimationFrame) window.requestAnimationFrame(run);
    setTimeout(run, 120);
  }
  function onResize() {
    // re-derive the float spot from the persisted position for the new
    // viewport (rotation / window resize never strands the orb off-screen)
    lastWhere = null; lastKey = null;
    onScroll();
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
    // double-click = forget the saved spot and re-sync to the dock
    // (the core re-inserts the wrap beside its anchor on send-home, so pull
    // it back out to <body> here too).
    wrap.addEventListener('dblclick', function () {
      ensureOnBody();
      dropPos();
      off.dx = 0; off.dy = 0;
      lastKey = null; lastWhere = null;
      setTimeout(function () { place(true); }, 60);
    });
    place(true);
    wrap.style.visibility = 'visible';

    // drag handoff for muse-orb.js: the core moves the wrap via transform
    // while dragging (transition killed), then hands the final offset here.
    // setOffset receives the TOTAL offset from the default corner and the
    // dock persists the absolute spot, so it survives reloads.
    window.MuseOrbDock = {
      getBase: function () { return base; },
      setOffset: function (dx, dy) {
        off.dx = Math.round(dx) || 0;
        off.dy = Math.round(dy) || 0;
        lastKey = null;
        if (base) storePos(base.x + off.dx, base.y + off.dy);
        place(true);
      },
      clearOffset: function () {
        off.dx = 0; off.dy = 0;
        dropPos();
        lastKey = null;
        place(true);
      }
    };
  }

  function boot() {
    wrap = $('.muse-orb-wrap');
    if (!wrap) { setTimeout(boot, 250); return; }
    wrap.style.visibility = 'hidden'; // avoid one-frame flash in the topbar flow
    takeOver();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onResize);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
