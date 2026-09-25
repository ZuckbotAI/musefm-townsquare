/* =========================================================================
 * orb-dock.js — the Muse orb's scroll lifecycle (2026-09-23 spec, Anthony).
 *
 * Three stages, in order:
 *  1. HERO — on page load, the orb sits in the hero beside the homepage
 *     headline, ~96px. Its home. Snaps into #hero-orb-stage when the stage
 *     is in view; click/tap it for a saying.
 *  2. DOCK — scroll past the hero and the orb moves to a docked "next
 *     slot": a fixed corner/side dock, smaller (~64px desktop / ~56px
 *     mobile), with a smooth fly transition.
 *  3. FOLLOW — keep scrolling past a second threshold and the orb breaks
 *     away from the dock and follows you: fixed, smaller still (~48px
 *     desktop / ~44px mobile), with a soft scroll-trailing lag and a
 *     gentle drift as it travels with you.
 *
 * The user can drag the orb anywhere (touch + mouse, via the pointer
 * handlers in muse-orb.js); the spot persists across reloads in
 * localStorage. Double-click returns the orb to its hero home (scrolls the
 * page back to the top on the homepage so the orb snaps into its stage;
 * re-syncs to the default corner on pages with no hero).
 *
 * The wrap is a direct child of <body> and moves via transform only, so it
 * never disturbs page layout. Its z-index sits below the miniplayer, the
 * mod bulk bar and other interactive chrome (style.css) — the orb never
 * covers media controls or the Shorts reaction rail.
 *
 * NEVER-COVER (hard requirement, 2026-09-23, Anthony): the orb must never
 * rest on anything tappable — media controls, buttons, links, inputs, the
 * Shorts reaction wheel. Every dock/follow resting spot (default corner,
 * drag drop, restored position) is probed with elementFromPoint and nudged
 * to the nearest clear spot when it would land on an interactive element.
 * prefers-reduced-motion: instant placement, no flying, no drift.
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
  // stage sizes stay appropriate per viewport (2026-09-23, Anthony)
  function dockScale() { return isMobile() ? 56 / ORB : 64 / ORB; }   // ~56px / ~64px
  function followScale() { return isMobile() ? 44 / ORB : 48 / ORB; } // ~44px / ~48px

  var reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  var wrap = null;
  var lastKey = null; // last placed transform, to avoid redundant writes
  var lastWhere = null; // last dock state
  var base = null; // un-offset float target {x, y, s} — drag math reads this
  var off = { dx: 0, dy: 0 }; // user offset from the default corner, px
  var savedPos = loadPos(); // persisted float spot {x, y} top-left px, or null
  var lastScrollY = -1; // for the follow-stage scroll lag
  var followLag = 0; // trailing offset while following, px (decays to 0)

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

  // ---- never-cover: the orb must never rest on anything tappable ----
  // Element selectors that count as "covered" when the orb's center lands
  // on them: media controls, buttons, links, inputs, the Shorts reaction
  // wheel and mute/actions, the miniplayer, the mod bulk bar, the topbar.
  // (Plain text/video surfaces don't block: a fixed overlay can't dodge
  // scrolled content, and the orb is small and round.)
  var COVER_SEL = 'button, a[href], input, select, textarea, summary,' +
    '[role="button"], [onclick], .sig, #miniplayer, .mod-bulkbar, .topbar,' +
    '.short-rxn, .short-mute, .short-actions';
  // The orb's own UI never counts as covered.
  var OWN_SEL = '.muse-orb-wrap, .muse-orb-panel, .muse-orb-nudge';

  function underOrbIsInteractive(cx, cy) {
    if (!wrap) return false;
    var prev = wrap.style.pointerEvents;
    wrap.style.pointerEvents = 'none'; // let elementFromPoint see beneath
    var el = null;
    try { el = document.elementFromPoint(cx, cy); } catch (e) { /* ignore */ }
    wrap.style.pointerEvents = prev;
    if (!el || el === document.body || el === document.documentElement) return false;
    if (el.closest && el.closest(OWN_SEL)) return false;
    return !!(el.closest && el.closest(COVER_SEL));
  }

  function spotClear(x, y, s) {
    var d = ORB * s;
    return !underOrbIsInteractive(x + d / 2, y + d / 2);
  }

  // nearest clear spot to (x, y): spiral outward, else null
  function findClearSpot(x, y, s) {
    var c0 = clampFloat(x, y, s);
    if (spotClear(c0.x, c0.y, s)) return c0;
    var step = Math.max(28, (ORB * s) / 2);
    for (var ring = 1; ring <= 6; ring++) {
      for (var k = 0; k < 8; k++) {
        var ang = (k / 8) * Math.PI * 2 + ring * 0.4;
        var c = clampFloat(x + Math.cos(ang) * ring * step,
                           y + Math.sin(ang) * ring * step, s);
        if (spotClear(c.x, c.y, s)) return c;
      }
    }
    return null;
  }

  // resolve a dock/follow resting spot: clamp on-screen, nudge off anything
  // tappable; fall back to the (clamped) smart default corner
  function resolveFloat(x, y, s) {
    var spot = findClearSpot(x, y, s);
    if (spot) return spot;
    var d = defaultFloat(s);
    return clampFloat(d.x, d.y, s);
  }

  // resolved smart default corner, cached per viewport+scale
  var cachedDefault = null;
  function getDefault(s) {
    var vw = window.innerWidth || 0, vh = window.innerHeight || 0;
    if (!cachedDefault || cachedDefault.s !== s ||
        cachedDefault.vw !== vw || cachedDefault.vh !== vh) {
      var d = defaultFloat(s);
      var rs = resolveFloat(d.x, d.y, s);
      cachedDefault = { x: rs.x, y: rs.y, s: s, vw: vw, vh: vh };
    }
    return cachedDefault;
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

  // second threshold: half a viewport past the hero's bottom edge. Past
  // this the orb breaks away from the dock and follows the user.
  function followAfter() {
    var vh = window.innerHeight || 800;
    var st = stage();
    if (st) {
      var r = st.getBoundingClientRect();
      var sy = window.scrollY || window.pageYOffset || 0;
      return r.bottom + sy + vh * 0.6;
    }
    return vh * 1.8;
  }

  // default dock/follow corner for this viewport: top-left px of the orb box
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
    // Service pages (Trustline etc.): the stage lives in .svc-orb-home and the
    // orb belongs in that bubble unconditionally — any scroll past 80px was
    // ejecting it to the float corner where it blocked text again
    // (2026-09-24, Anthony: "orb not snapping into place").
    var sticky = !!(st && st.closest && st.closest('.svc-orb-home'));
    if (st && (sticky || stageInView())) {
      // HERO: snap into the hero card stage for click-for-sayings
      var r = st.getBoundingClientRect();
      // guard: a zero-size stage (stylesheet failed) must not emit scale(0)
      if (r.width > 1) {
        var s = Math.min(1.5, (r.width / ORB) * 0.85);
        return {
          x: r.left + (r.width - ORB * s) / 2,
          y: r.top + (r.height - ORB * s) / 2,
          s: s, where: 'hero'
        };
      }
    }
    // past the hero: DOCK first, then FOLLOW once past the second threshold
    var sy = window.scrollY || window.pageYOffset || 0;
    var fa = followAfter();
    var where = sy < fa ? 'dock' : 'follow';
    var fs = where === 'dock' ? dockScale() : followScale();
    var gd = getDefault(fs);
    return { x: gd.x, y: gd.y, s: fs, where: where };
  }

  function place(instant) {
    if (!wrap) return;
    // hands off while the user is mid-drag — muse-orb.js drives the transform
    if (wrap.classList.contains('muse-orb-dragging')) return;
    var t = target();
    if (t.where !== lastWhere) {
      if ((t.where === 'dock' || t.where === 'follow') && savedPos) {
        // restore the user's persisted spot — nudged off anything tappable
        // in case the layout changed since it was saved
        var rs = resolveFloat(savedPos.x, savedPos.y, t.s);
        off.dx = rs.x - t.x;
        off.dy = rs.y - t.y;
      } else {
        // hero snap, or a fresh dock/follow with no saved spot: no offset
        off.dx = 0; off.dy = 0;
      }
      if (t.where !== 'follow') followLag = 0; // leaving the follower: settle
      lastWhere = t.where;
    }
    base = { x: t.x, y: t.y, s: t.s };
    var x = Math.round(t.x + off.dx);
    var y = Math.round(t.y + off.dy);
    if (t.where === 'follow' && !reduced) {
      // the follower trails the scroll: it lags behind the target, then
      // eases back — the "traveling with you" feel (2026-09-23, Anthony)
      followLag *= 0.86;
      y = Math.round(t.y + off.dy + followLag);
    }
    var key = t.where + ':' + x + ',' + y + ',' + t.s;
    if (key === lastKey && !instant) return;
    lastKey = key;
    wrap.classList.toggle('muse-orb-follow', t.where === 'follow');
    wrap.style.transform = 'translate(' + x + 'px,' + y + 'px) scale(' + t.s + ')';
    wrap.setAttribute('data-orb-where', t.where);
  }

  // while the follower's lag is settling, keep easing it to zero so the
  // orb glides back onto its spot after a scroll burst ends
  function settleFollow() {
    if (Math.abs(followLag) < 0.5 || lastWhere !== 'follow' ||
        wrap.classList.contains('muse-orb-dragging')) {
      followLag = 0;
      return;
    }
    followLag *= 0.88;
    place(true);
    setTimeout(settleFollow, 50);
  }

  var ticking = false;
  function onScroll() {
    // feed the follow-stage lag from the scroll delta before placing
    var sy = window.scrollY || window.pageYOffset || 0;
    if (lastScrollY >= 0 && !reduced) {
      var d = sy - lastScrollY;
      followLag = Math.max(-110, Math.min(110, followLag + d * 0.35));
    }
    lastScrollY = sy;
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
      if (lastWhere === 'follow' && Math.abs(followLag) >= 0.5) settleFollow();
    }
    if (window.requestAnimationFrame) window.requestAnimationFrame(run);
    setTimeout(run, 120);
  }
  function onResize() {
    // re-derive the dock/follow spot from the persisted position for the new
    // viewport (rotation / window resize never strands the orb off-screen)
    lastWhere = null; lastKey = null; cachedDefault = null; lastScrollY = -1;
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
    // double-click = return to the hero home (2026-09-23, Anthony): forget
    // the saved spot and glide the page back to the top so the orb snaps
    // into its hero bubble. (The core re-inserts the wrap beside its anchor
    // on send-home, so pull it back out to <body> here too.) On pages with
    // no hero stage, re-sync to the default corner.
    wrap.addEventListener('dblclick', function () {
      ensureOnBody();
      dropPos();
      off.dx = 0; off.dy = 0;
      lastKey = null; lastWhere = null;
      followLag = 0;
      if (stage()) {
        try {
          if (reduced) window.scrollTo(0, 0);
          else window.scrollTo({ top: 0, behavior: 'smooth' });
        } catch (e) { window.scrollTo(0, 0); }
      }
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
        // drop: persist the NEAREST CLEAR spot, nudged off anything
        // tappable — the orb visibly slides there via the fly transition
        if (base) {
          var rawX = base.x + (Math.round(dx) || 0);
          var rawY = base.y + (Math.round(dy) || 0);
          var rs = resolveFloat(rawX, rawY, base.s);
          off.dx = Math.round(rs.x - base.x);
          off.dy = Math.round(rs.y - base.y);
          storePos(rs.x, rs.y);
        } else {
          off.dx = Math.round(dx) || 0;
          off.dy = Math.round(dy) || 0;
        }
        lastKey = null;
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
    // late layout shifts (fonts, images) and iOS toolbar show/hide can move
    // the stage after boot — re-sync once things settle (2026-09-24)
    window.addEventListener('load', function () { lastKey = null; place(false); });
    if (window.visualViewport) window.visualViewport.addEventListener('resize', onResize);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
