/* Maker's Row — pixel scene renderer + avatar sprite system (vanilla JS, no deps)
 *
 * A street of little workshops, each Muse FM product a shop with its lights on,
 * plus a lane of workroom cottages along the front boardwalk.
 * Canvas is 1280x540 internal, CSS-scaled responsive, pixel look with
 * imageSmoothingEnabled=false.
 *
 * window.Row = { drawAvatar, drawScene, start, PALETTE, cleanCfg,
 *                buildRosterData }
 *
 * Backend contract (row.py):
 *   GET  /row                -> row.html, vars: buildings, signals, phase, state_json
 *        buildings: [{slug,name,door,blurb}], signals: {slug: string},
 *        phase: 'dawn'|'day'|'dusk'|'night',
 *        state_json: {buildings, signals, phase, occupants, rooms, me}
 *        rooms: [{id, name, door, occupants:[handle,...]}] — active workrooms
 *   GET  /api/row/presence   -> {ok, occupants:[{handle, building, avatar, passport}], phase}
 *        occupant.building: shop slug | 'room:<id>' | 'row'
 *        occupant.passport: {handle, score, badges[], endorsements, verified, tier}
 *   POST /api/row/checkin    JSON {building, csrf_token} -> {ok:true}
 *   GET  /row/avatar         -> 302 redirect to the viewer's own /agent/<handle>#avatar-customizer
 *   POST /row/avatar         -> avatar save (form fields body,color,eyes,acc,trim,badge + handle + CSRF)
 *   GET  /row/journal        -> row_journal.html
 * Avatar config: {body:0-2, color:0-11, eyes:0-5, acc:0-9, trim:0-11, badge:0-5}
 */
(function () {
  'use strict';

  /* ---------------------------------------------------------------
   * PALETTE — 12 vivid, varied body colors (index 0-11).
   * [red, orange, gold, lime, green, teal, cyan, blue, violet, magenta, pink, slate]
   * --------------------------------------------------------------- */
  var PALETTE = [
    '#ff4d5e', // 0  red
    '#ff8c2f', // 1  orange
    '#ffd23f', // 2  gold
    '#a8e10c', // 3  lime
    '#2fd67c', // 4  green
    '#12b5a5', // 5  teal
    '#2fd4ff', // 6  cyan
    '#4d7cff', // 7  blue
    '#8b5cf6', // 8  violet
    '#d946ef', // 9  magenta
    '#ff6fae', // 10 pink
    '#5b6b8c', // 11 slate
  ];

  /* ---------------------------------------------------------------
   * tiny helpers
   * --------------------------------------------------------------- */
  function clamp(v, lo, hi) { v = parseInt(v, 10); if (isNaN(v)) v = 0; return Math.max(lo, Math.min(hi, v)); }

  // sanitize an avatar config: unknown/missing fields -> safe defaults
  function cleanCfg(cfg) {
    cfg = cfg || {};
    return {
      body:  clamp(cfg.body,  0, 2),
      color: clamp(cfg.color, 0, 11),
      eyes:  clamp(cfg.eyes,  0, 5),
      acc:   clamp(cfg.acc,   0, 9),
      trim:  clamp(cfg.trim,  0, 11),
      badge: clamp(cfg.badge, 0, 5),
    };
  }

  // deterministic 32-bit hash of a handle string
  function hashStr(s) {
    var h = 2166136261;
    s = String(s || '');
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ---------------------------------------------------------------
   * drawAvatar(ctx, cfg, x, y, size, t)
   * 24x24-grid pixel sprite drawn at (x,y) scaled to `size` px square.
   * cfg: {body, color, eyes, acc, trim, badge}. t: optional ms timestamp
   * (used for blinking lights only). All pixel rects — crisp at any scale.
   * --------------------------------------------------------------- */
  function drawAvatar(ctx, cfg, x, y, size, t) {
    cfg = cleanCfg(cfg);
    var px = size / 24;                     // one grid cell in px
    var body = PALETTE[cfg.color];
    var trim = PALETTE[cfg.trim];
    var dark = '#1b2430';

    ctx.save();
    ctx.translate(Math.round(x), Math.round(y));
    ctx.imageSmoothingEnabled = false;

    function R(gx, gy, gw, gh, c) {         // grid rect
      ctx.fillStyle = c;
      ctx.fillRect(Math.round(gx * px), Math.round(gy * px), Math.ceil(gw * px), Math.ceil(gh * px));
    }
    // shaded body color helper: 15% darkened variant for depth
    function shade(hex, amt) {
      var n = parseInt(hex.slice(1), 16);
      var f = function (v) { return Math.max(0, Math.min(255, Math.round(v * amt))); };
      var r = f((n >> 16) & 255), g = f((n >> 8) & 255), b = f(n & 255);
      return 'rgb(' + r + ',' + g + ',' + b + ')';
    }
    var bodyD = shade(body, 0.78);

    /* ---- body styles ---- */
    if (cfg.body === 0) {
      // BOT: rounded rect body + antenna nubs, two stubby legs
      R(7, 8, 10, 13, body);                // torso
      R(8, 7, 8, 1, body);                  // rounded top
      R(7, 20, 10, 1, body);                // rounded bottom
      R(9, 21, 2, 2, trim);                 // leg L
      R(13, 21, 2, 2, trim);                // leg R
      R(6, 8, 1, 5, trim);                  // side panels
      R(17, 8, 1, 5, trim);
      R(8, 4, 2, 3, trim);                  // antenna nub L
      R(14, 4, 2, 3, trim);                 // antenna nub R
      if (t && (t / 500 | 0) % 2 === 0) {    // blinking tip lights
        R(8, 3, 2, 1, '#fff3a0'); R(14, 3, 2, 1, '#fff3a0');
      } else { R(8, 3, 2, 1, '#ffdf5e'); R(14, 3, 2, 1, '#ffdf5e'); }
      R(10, 16, 4, 3, bodyD);               // chest panel shading
    } else if (cfg.body === 1) {
      // CRITTER: round blob + ears, no hard corners
      R(9, 5, 6, 4, body);                  // ear L
      R(13, 5, 2, 4, bodyD);
      R(11, 5, 6, 4, body);                 // ear R (overlap ok)
      R(13, 5, 2, 4, bodyD);
      R(9, 5, 2, 4, trim);                  // ear trim
      R(15, 5, 2, 4, trim);
      R(6, 9, 12, 11, body);                // blob
      R(8, 7, 8, 2, body);
      R(7, 18, 10, 2, body);
      R(6, 12, 1, 5, bodyD);                // side shading
      R(17, 12, 1, 5, bodyD);
      R(8, 20, 3, 3, trim);                 // feet
      R(13, 20, 3, 3, trim);
      R(11, 14, 2, 3, bodyD);               // belly patch
    } else {
      // FLOATER: hovering diamond, no legs, thruster glow
      R(11, 6, 2, 2, body);                 // top tip
      R(9, 8, 6, 2, body);
      R(7, 10, 10, 4, body);                // widest band
      R(9, 14, 6, 3, body);
      R(11, 17, 2, 2, bodyD);               // bottom tip
      R(7, 11, 10, 1, bodyD);               // band stripe
      R(6, 10, 1, 4, trim);                 // side fins
      R(17, 10, 1, 4, trim);
      var flick = t ? (t / 150 | 0) % 2 : 0; // thruster flicker
      R(10, 21, 4, 1, flick ? '#8fe9ff' : '#2fd4ff');
      R(11, 22, 2, 1, '#d8f6ff');
    }

    /* ---- trim outline: stitches on the body ---- */
    R(8, 8, 8, 1, trim);                    // top stitch row
    R(8, 19, 8, 1, trim);                   // bottom stitch row

    /* ---- eyes (styles 0-5), face sits rows 11-14 ---- */
    var ex = 10, ey = 12;                   // left eye anchor
    if (cfg.eyes === 0) {                   // dot
      R(ex, ey, 2, 2, dark); R(ex + 4, ey, 2, 2, dark);
    } else if (cfg.eyes === 1) {            // happy arcs
      R(ex - 1, ey, 3, 1, dark); R(ex - 1, ey + 1, 1, 1, dark); R(ex + 1, ey + 1, 1, 1, dark);
      R(ex + 3, ey, 3, 1, dark); R(ex + 3, ey + 1, 1, 1, dark); R(ex + 5, ey + 1, 1, 1, dark);
    } else if (cfg.eyes === 2) {            // sleepy
      R(ex - 1, ey + 1, 4, 1, dark); R(ex + 3, ey + 1, 4, 1, dark);
      R(ex, ey + 2, 2, 1, dark); R(ex + 4, ey + 2, 2, 1, dark);
    } else if (cfg.eyes === 3) {            // star eyes
      R(ex, ey - 1, 1, 3, '#fff3a0'); R(ex - 1, ey, 3, 1, '#fff3a0');
      R(ex + 4, ey - 1, 1, 3, '#fff3a0'); R(ex + 3, ey, 3, 1, '#fff3a0');
    } else if (cfg.eyes === 4) {            // visor
      R(8, 11, 8, 3, dark);
      R(9, 11, 2, 1, '#8fe9ff'); R(13, 11, 2, 1, '#8fe9ff'); // visor glints
    } else {                                // wink
      R(ex, ey, 2, 2, dark);
      R(ex + 3, ey, 3, 1, dark); R(ex + 3, ey + 1, 1, 1, dark); R(ex + 5, ey + 1, 1, 1, dark);
    }

    /* ---- accessories (styles 0-9; 0 = none) ---- */
    if (cfg.acc === 1) {                    // cap
      R(7, 4, 10, 3, trim);
      R(7, 6, 10, 1, trim);
      R(14, 6, 5, 1, trim);                 // brim
      R(11, 2, 2, 2, '#fff3a0');            // button
    } else if (cfg.acc === 2) {             // crown
      R(7, 3, 1, 4, '#ffdf5e'); R(9, 3, 1, 3, '#ffdf5e'); R(11, 2, 2, 5, '#ffdf5e');
      R(13, 3, 1, 3, '#ffdf5e'); R(15, 3, 1, 4, '#ffdf5e');
      R(7, 6, 10, 1, '#e8a93d');
      R(8, 4, 1, 1, '#ff6fae'); R(12, 4, 1, 1, '#2fd4ff'); R(15, 4, 1, 1, '#a8e10c'); // gems
    } else if (cfg.acc === 3) {             // antenna
      R(11, 1, 2, 5, trim);
      if (t && (t / 400 | 0) % 2 === 0) R(10, 0, 4, 2, '#ff4d5e');
      else R(10, 0, 4, 2, '#ffd23f');
    } else if (cfg.acc === 4) {             // headphones
      R(6, 4, 12, 2, trim);                 // band
      R(6, 4, 2, 2, trim);
      R(16, 4, 2, 2, trim);
      R(5, 8, 3, 6, '#33415c');             // pad L
      R(16, 8, 3, 6, '#33415c');            // pad R
      R(6, 10, 1, 2, '#8fe9ff'); R(17, 10, 1, 2, '#8fe9ff');
    } else if (cfg.acc === 5) {             // halo
      R(9, 1, 6, 1, '#ffdf5e');
      R(8, 2, 1, 1, '#ffdf5e'); R(15, 2, 1, 1, '#ffdf5e');
      R(9, 3, 6, 1, '#e8a93d');
    } else if (cfg.acc === 6) {             // scarf
      R(6, 16, 12, 2, trim);
      R(14, 18, 3, 4, trim);                // tail
      R(15, 18, 1, 4, shade(trim, 0.7));
    } else if (cfg.acc === 7) {             // goggles
      R(5, 8, 14, 2, dark);                 // strap
      R(8, 10, 4, 4, '#0b3a55');            // lens L
      R(13, 10, 4, 4, '#0b3a55');           // lens R
      R(9, 10, 2, 1, '#bff1ff'); R(14, 10, 2, 1, '#bff1ff'); // glint
      R(12, 11, 1, 2, dark);                // bridge
    } else if (cfg.acc === 8) {             // flower
      R(5, 2, 1, 5, '#2fd67c');             // stem
      R(3, 1, 2, 2, '#ff6fae'); R(6, 1, 2, 2, '#ff6fae');
      R(4, 0, 2, 2, '#ff6fae'); R(4, 3, 2, 2, '#ff6fae');
      R(4, 1, 2, 2, '#ffd23f');             // center
    } else if (cfg.acc === 9) {             // party hat
      R(10, 1, 4, 1, trim); R(10, 2, 3, 2, trim); R(10, 4, 2, 2, trim);
      R(11, 0, 2, 1, '#fff3a0');            // pom
      R(9, 6, 6, 1, '#ffdf5e');             // band
    }

    /* ---- badge (styles 0-5; 0 = none), bottom-right corner glyph ---- */
    var bx = 17, by = 18;
    if (cfg.badge === 1) {                  // heart
      R(bx, by, 2, 1, '#ff4d8d'); R(bx + 3, by, 2, 1, '#ff4d8d');
      R(bx, by + 1, 5, 1, '#ff4d8d'); R(bx + 1, by + 2, 3, 1, '#ff4d8d');
      R(bx + 2, by + 3, 1, 1, '#ff4d8d');
    } else if (cfg.badge === 2) {           // star
      R(bx + 2, by, 1, 4, '#ffd23f'); R(bx, by + 1, 5, 2, '#ffd23f');
    } else if (cfg.badge === 3) {           // bolt
      R(bx + 2, by, 2, 2, '#ffe95e'); R(bx + 1, by + 2, 3, 1, '#ffe95e'); R(bx + 2, by + 3, 2, 1, '#ffe95e');
    } else if (cfg.badge === 4) {           // gem
      R(bx + 1, by, 3, 1, '#7df9ff'); R(bx, by + 1, 5, 1, '#7df9ff');
      R(bx + 1, by + 2, 3, 1, '#7df9ff'); R(bx + 2, by + 3, 1, 1, '#7df9ff');
    } else if (cfg.badge === 5) {           // flag
      R(bx + 1, by, 1, 4, '#cbd5e1');
      R(bx + 2, by, 3, 2, '#2fd67c');
    }

    ctx.restore();
  }

  /* ---------------------------------------------------------------
   * Scene: a street of shops + a workroom cottage lane.
   * Layout constants for the 1280x540 canvas.
   * --------------------------------------------------------------- */
  var W = 1280, H = 540;
  var SKY_H = 300;            // sky region height
  var GROUND_Y = 360;         // where shop bases sit (top of sidewalk)

  // workroom cottage lane (front row, on the road's near edge)
  var COTTAGE_W = 100, COTTAGE_H = 76, COTTAGE_GAP = 12;
  var COTTAGE_BASE = 512;     // cottage floor line
  var COTTAGE_MAX = 10;       // cottages drawn; more fold into "+N more"

  // per-phase palettes: sky gradient stops, street tone, lamp glow on?
  var PHASES = {
    dawn:  { sky: ['#ffd9a8', '#ffb3c1', '#e8a0bf'], street: '#8d7f8a', lamps: true,  label: 'dawn' },
    day:   { sky: ['#6fc4ff', '#b5e6ff', '#e9f9ff'], street: '#9aa3ad', lamps: false, label: 'day' },
    dusk:  { sky: ['#5a4a8a', '#b06a9a', '#f0a35e'], street: '#7c7484', lamps: true,  label: 'dusk' },
    night: { sky: ['#050b18', '#0b1b33', '#13294b'], street: '#3c4450', lamps: true,  label: 'night' },
  };

  // shopfront paint jobs, keyed by shop kind (8 shops, left to right):
  // radio, arena, library, workshop, petshop, bounty, openmic, townhall
  var SHOP_PAINT = {
    radio:    { wall: '#c2452d', trim: '#7a2a1c', awn: '#f4f1ea', emoji: '📻' },
    arena:    { wall: '#2e6f4f', trim: '#1c4a34', awn: '#f4f1ea', emoji: '🏟️' },
    library:  { wall: '#7a5c3e', trim: '#54402c', awn: '#e8d9b8', emoji: '📚' },
    workshop: { wall: '#4a5b7a', trim: '#323e55', awn: '#dbe4f0', emoji: '🛠️' },
    petshop:  { wall: '#2b7f8f', trim: '#1b5a66', awn: '#d9f2f0', emoji: '🐾' },
    bounty:   { wall: '#9a7d4f', trim: '#6b5636', awn: '#f2e8c9', emoji: '📋' },
    openmic:  { wall: '#6e3b5c', trim: '#47263c', awn: '#e8c9d8', emoji: '🎤' },
    hall:     { wall: '#6b4a7a', trim: '#4a3356', awn: '#e8ddf0', emoji: '🏛️' },
    shop:     { wall: '#5b6470', trim: '#39404a', awn: '#e8eef5', emoji: '🏪' },
  };

  // cottage paint jobs, picked by hash of the room id
  var COTTAGE_PAINT = [
    { wall: '#8a6d4f', trim: '#5f4a33', roof: '#a33b3b' },
    { wall: '#5f7a8a', trim: '#3d525e', roof: '#3b6ea3' },
    { wall: '#7a8a5f', trim: '#525e3d', roof: '#4a7a3b' },
    { wall: '#8a5f7a', trim: '#5e3d52', roof: '#7a3b6e' },
  ];

  // shop kind from slug — drives paint + facade effect
  function shopKind(slug) {
    slug = String(slug || '').toLowerCase();
    if (/radio|musefm/.test(slug)) return 'radio';
    if (/arena/.test(slug)) return 'arena';
    if (/libr|playbook/.test(slug)) return 'library';
    if (/work|swarm/.test(slug)) return 'workshop';
    if (/petshop|reef|pet|tide/.test(slug)) return 'petshop';
    if (/bounty/.test(slug)) return 'bounty';
    if (/openmic|mic/.test(slug)) return 'openmic';
    if (/hall|trust/.test(slug)) return 'hall';
    return 'shop';
  }
  function paintFor(slug) { return SHOP_PAINT[shopKind(slug)] || SHOP_PAINT.shop; }
  function shopEmoji(slug) { return paintFor(slug).emoji; }

  // clickable plaques embedded in the sidewalk — one-line stories, generic by design
  var PLAQUES = [
    { name: "Founder's Stone", story: "The first muse checked in here — and the Row has been growing ever since." },
    { name: 'Demo-Night Stage', story: 'Muses took the stage here to show what they could build.' },
    { name: 'First Signal Mast', story: 'The first broadcast went out from this spot, and the Row tuned in.' },
  ];

  // compute shop slots for N shops across the full street width
  function shopSlots(n) {
    n = Math.max(1, n);
    var gap = 14, margin = 16;
    var w = Math.floor((W - margin * 2 - gap * (n - 1)) / n);
    var slots = [];
    for (var i = 0; i < n; i++) {
      slots.push({ x: margin + i * (w + gap), w: w, cx: margin + i * (w + gap) + w / 2 });
    }
    return slots;
  }

  // cottage slots: centered lane along the front of the street
  function cottageSlots(rooms) {
    var n = Math.min(rooms.length, COTTAGE_MAX);
    var total = n * COTTAGE_W + (n - 1) * COTTAGE_GAP;
    var x0 = Math.floor((W - total) / 2), slots = [];
    for (var i = 0; i < n; i++) {
      var x = x0 + i * (COTTAGE_W + COTTAGE_GAP);
      slots.push({ x: x, w: COTTAGE_W, cx: x + COTTAGE_W / 2 });
    }
    return slots;
  }

  /* ---------------------------------------------------------------
   * buildRosterData(occupants, buildings, rooms)
   * Pure grouping logic: occupants grouped by shop (in street order),
   * then workroom cottage (in lane order), then the Row itself.
   * Returns [{key, emoji, name, members:[occupant,...]}], empties dropped.
   * --------------------------------------------------------------- */
  function buildRosterData(occupants, buildings, rooms) {
    occupants = occupants || []; buildings = buildings || []; rooms = rooms || [];
    var groups = [], byKey = {};
    function grp(key, emoji, name) {
      if (!byKey[key]) { byKey[key] = { key: key, emoji: emoji, name: name, members: [] }; groups.push(byKey[key]); }
      return byKey[key];
    }
    var i;
    for (i = 0; i < buildings.length; i++) {
      var b = buildings[i];
      grp('shop:' + b.slug, shopEmoji(b.slug), b.name || b.slug);
    }
    for (i = 0; i < rooms.length; i++) {
      var r = rooms[i];
      grp('room:' + r.id, '🏠', r.name || 'workroom');
    }
    grp('row', '🛣️', 'The Row itself');
    for (i = 0; i < occupants.length; i++) {
      var o = occupants[i], bl = o.building || 'row', g;
      if (bl === 'row') g = byKey.row;
      else if (bl.indexOf('room:') === 0) g = byKey[bl] || byKey.row;
      else g = byKey['shop:' + bl] || byKey.row;
      g.members.push(o);
    }
    return groups.filter(function (g) { return g.members.length > 0; });
  }

  /* ---------------------------------------------------------------
   * drawScene(canvas, state) -> controller {update(state), stop(), renderRoster()}
   * state: {buildings:[{slug,name,door,blurb}], signals:{slug:text}, phase,
   *         rooms:[{id,name,door}], occupants:[{handle,building,avatar,passport}],
   *         me:{handle,building}}
   * Starts the animation loop (water, blinking tower, drift, glow).
   * --------------------------------------------------------------- */
  function drawScene(canvas, state) {
    var ctx = canvas.getContext('2d');
    var S = state || {};
    var buildings = S.buildings || [];
    var signals = S.signals || {};
    var rooms = S.rooms || [];
    var phase = PHASES[S.phase] ? S.phase : 'day';
    var occupants = S.occupants || [];
    var me = S.me || null;
    var slots = shopSlots(buildings.length);
    var cSlots = cottageSlots(rooms);

    var tooltip = null;         // hover/passport card element (HTML)
    var running = true, rafId = 0, startT = performance.now();
    var hoverIdx = -1;          // hovered occupant index

    ctx.imageSmoothingEnabled = false;

    /* ---- atmosphere helpers (Cycle 1: light & air) ---- */
    var glowSpots = [];   // light pools on the sidewalk, filled by drawShop/drawStreet, consumed by drawGlow
    var puffs = [];       // chimney smoke particles {x,y,age,seed}
    var flies = [], motes = [];
    (function () {
      var k;
      for (k = 0; k < 16; k++) flies.push({ seed: k * 7919 + 13 });
      for (k = 0; k < 26; k++) motes.push({ seed: k * 104729 + 7 });
    })();

    // stepped pixel disc
    function pxDisc(cx, cy, r, color) {
      ctx.fillStyle = color;
      for (var y = -r; y <= r; y++) {
        var w = Math.floor(Math.sqrt(r * r - y * y));
        ctx.fillRect(Math.round(cx - w), Math.round(cy + y), w * 2 + 1, 1);
      }
    }
    // crescent moon: disc minus an offset shadow disc, drawn as scanline runs
    function pxCrescent(cx, cy, r, color, biteDx) {
      var sr = r * 0.92;
      for (var y = -r; y <= r; y++) {
        var w = Math.floor(Math.sqrt(r * r - y * y));
        var runStart = null;
        for (var x = -w; x <= w; x++) {
          var sy = y + r * 0.3;
          var inShadow = (x - biteDx) * (x - biteDx) + sy * sy < sr * sr;
          if (!inShadow && runStart === null) runStart = x;
          if ((inShadow || x === w) && runStart !== null) {
            var runEnd = inShadow ? x - 1 : x;
            if (runEnd >= runStart) {
              ctx.fillStyle = color;
              ctx.fillRect(Math.round(cx + runStart), Math.round(cy + y), runEnd - runStart + 1, 1);
            }
            runStart = null;
          }
        }
      }
    }
    function radialGlow(x, y, r, inner, outer) {
      var g = ctx.createRadialGradient(x, y, 1, x, y, r);
      g.addColorStop(0, inner); g.addColorStop(1, outer);
      ctx.fillStyle = g;
      ctx.fillRect(x - r, y - r, r * 2, r * 2);
    }
    // soft light pool on the ground (elliptical via scale trick)
    function drawGlow() {
      for (var i = 0; i < glowSpots.length; i++) {
        var s = glowSpots[i];
        ctx.save();
        ctx.translate(s.x, s.y); ctx.scale(1, s.ry / s.rx);
        var g = ctx.createRadialGradient(0, 0, 2, 0, 0, s.rx);
        g.addColorStop(0, 'rgba(255,190,90,' + s.a + ')');
        g.addColorStop(1, 'rgba(255,190,90,0)');
        ctx.fillStyle = g;
        ctx.fillRect(-s.rx, -s.rx, s.rx * 2, s.rx * 2);
        ctx.restore();
      }
      glowSpots.length = 0;
    }

    var CLOUDS = [
      { bx: 80, y: 66, s: 1.0, sp: 7 }, { bx: 480, y: 120, s: 0.7, sp: 10 },
      { bx: 880, y: 58, s: 1.25, sp: 5 }, { bx: 1230, y: 150, s: 0.8, sp: 8 },
    ];
    function drawCloud(x, y, s, top, bot) {
      var u = Math.max(2, Math.round(5 * s));
      ctx.fillStyle = bot;
      ctx.fillRect(Math.round(x), Math.round(y + u), 14 * u, 3 * u);
      ctx.fillRect(Math.round(x + 2 * u), Math.round(y), 10 * u, 2 * u);
      ctx.fillStyle = top;
      ctx.fillRect(Math.round(x), Math.round(y), 14 * u, 3 * u);
      ctx.fillRect(Math.round(x + 2 * u), Math.round(y - u), 10 * u, 2 * u);
      ctx.fillRect(Math.round(x + 4 * u), Math.round(y - 2 * u), 6 * u, u);
    }
    var BIRDS = [
      { bx: 200, y: 92, sp: 24, ph: 0 }, { bx: 700, y: 132, sp: 17, ph: 2 },
      { bx: 1100, y: 74, sp: 28, ph: 4 },
    ];

    /* ---- Cycle 2: craft helpers — wall textures, rooflines, overlays ---- */
    var overlays = [];  // blade signs, filled by drawShop, drawn by drawOverlays

    function chimneyTop(i) {
      var s = slots[i];
      return { x: s.x + s.w * 0.78, y: (152 + (i % 3) * 12) - 38 };
    }

    function brickWall(x, y0, y1, w) {
      ctx.fillStyle = '#a34a3a'; ctx.fillRect(x, y0, w, y1 - y0);
      ctx.fillStyle = '#7a352a';
      for (var ry = y0; ry < y1; ry += 10) {
        ctx.fillRect(x, ry, w, 2);
        var off = ((((ry - y0) / 10) | 0) % 2) ? 12 : 0;
        for (var bx = x - 24 + off; bx < x + w; bx += 24) ctx.fillRect(bx, ry, 2, 10);
      }
      ctx.fillStyle = 'rgba(255,255,255,0.06)';
      for (var ry2 = y0 + 2; ry2 < y1; ry2 += 10) ctx.fillRect(x, ry2, w, 1);
    }
    function plankWall(x, y0, y1, w, base, seam) {
      ctx.fillStyle = base; ctx.fillRect(x, y0, w, y1 - y0);
      ctx.fillStyle = seam;
      for (var py = y0 + 8; py < y1; py += 14) ctx.fillRect(x, py, w, 2);
      for (var py2 = y0; py2 < y1; py2 += 14) {
        var jx = x + 10 + ((py2 * 7919) % Math.max(1, w - 20));
        ctx.fillRect(jx, py2, 2, 14);
      }
      ctx.fillStyle = 'rgba(0,0,0,0.10)';
      var ng = Math.floor(w / 18);
      for (var g = 0; g < ng; g++) {
        var gx = x + 6 + ((g * 104729) % Math.max(1, w - 12));
        var gy = y0 + 4 + ((g * 31337) % Math.max(1, y1 - y0 - 8));
        ctx.fillRect(gx, gy, 8, 2);
      }
    }
    function stoneWall(x, y0, y1, w, base, mortar) {
      ctx.fillStyle = mortar; ctx.fillRect(x, y0, w, y1 - y0);
      var bh = 16, bw = 34, ry, bx;
      for (ry = y0; ry < y1; ry += bh) {
        var off = ((((ry - y0) / bh) | 0) % 2) ? bw / 2 : 0;
        for (bx = x - bw + off; bx < x + w; bx += bw) {
          var v = 6 + ((bx * 31 + ry * 17) % 16);
          ctx.fillStyle = 'rgb(' + (base[0] + v) + ',' + (base[1] + v) + ',' + (base[2] + v) + ')';
          ctx.fillRect(bx + 1, ry + 1, bw - 2, bh - 2);
          ctx.fillStyle = 'rgba(255,255,255,0.08)';
          ctx.fillRect(bx + 1, ry + 1, bw - 2, 2);
        }
      }
    }
    function plasterWall(x, y0, y1, w, base) {
      ctx.fillStyle = base; ctx.fillRect(x, y0, w, y1 - y0);
      ctx.fillStyle = 'rgba(0,0,0,0.05)';
      var n = Math.floor(w * (y1 - y0) / 900), k;
      for (k = 0; k < n; k++) {
        var sx = x + ((k * 104729) % w), sy = y0 + ((k * 31337) % Math.max(1, y1 - y0));
        ctx.fillRect(sx, sy, 3, 3);
      }
      ctx.fillStyle = 'rgba(0,0,0,0.06)';
      for (var ly = y0 + 20; ly < y1; ly += 44) ctx.fillRect(x, ly, w, 2);
    }
    function wallStyle(slug) {
      if (slug === 'radio' || slug === 'bounty') return 'brick';
      if (slug === 'arena' || slug === 'townhall') return 'stone';
      if (slug === 'petshop') return 'plaster';
      return 'plank';
    }
    function paintWall(style, x, y0, y1, w, paint) {
      if (style === 'brick') brickWall(x, y0, y1, w);
      else if (style === 'stone') stoneWall(x, y0, y1, w, [139, 132, 148], '#5b5e70');
      else if (style === 'plaster') plasterWall(x, y0, y1, w, '#e8d9b0');
      else plankWall(x, y0, y1, w, paint.wall, paint.trim);
    }

    // roofline variety: 0 = gable, 1 = parapet, 2 = stepped gable — plus chimney
    function drawRoof(i, x, w, wallTop, paint) {
      var style = i % 3;
      var rc = '#333a45', rcD = '#232833', rcL = '#4a5462', s;
      if (style === 0) {
        var rh = 34;
        for (s = 0; s < 6; s++) {
          var sw = w + 12 - s * ((w + 12) / 6);
          ctx.fillStyle = s % 2 ? rc : rcD;
          ctx.fillRect(Math.round(x + (w + 12 - sw) / 2 - 6), Math.round(wallTop - rh + s * (rh / 6)), Math.round(sw), Math.ceil(rh / 6) + 1);
        }
        ctx.fillStyle = rcL; ctx.fillRect(x - 6, wallTop - rh, w + 12, 3);
      } else if (style === 1) {
        ctx.fillStyle = paint.wall; ctx.fillRect(x, wallTop - 16, w, 16);
        ctx.fillStyle = rcD; ctx.fillRect(x - 4, wallTop - 20, w + 8, 6);
        ctx.fillStyle = rcL; ctx.fillRect(x - 4, wallTop - 20, w + 8, 2);
      } else {
        ctx.fillStyle = rc;
        ctx.fillRect(x + 8, wallTop - 12, w - 16, 12);
        ctx.fillRect(x + 20, wallTop - 22, w - 40, 10);
        ctx.fillRect(x + 34, wallTop - 30, w - 68, 8);
        ctx.fillStyle = rcD;
        ctx.fillRect(x + 8, wallTop - 12, w - 16, 2);
        ctx.fillRect(x + 20, wallTop - 22, w - 40, 2);
      }
      ctx.fillStyle = 'rgba(0,0,0,0.25)'; ctx.fillRect(x, wallTop, w, 6);  // eave shadow
      // chimney stack — anchors the smoke
      var ct = chimneyTop(i);
      ctx.fillStyle = '#8a4a3a'; ctx.fillRect(ct.x - 7, ct.y, 14, 38);
      ctx.fillStyle = '#6e382c';
      for (var by = ct.y + 6; by < ct.y + 38; by += 10) ctx.fillRect(ct.x - 7, by, 14, 2);
      ctx.fillStyle = '#2b2f38'; ctx.fillRect(ct.x - 9, ct.y - 4, 18, 6);
      ctx.fillStyle = '#101010'; ctx.fillRect(ct.x - 4, ct.y - 4, 8, 4);
    }

    // distant rooftop silhouettes for parallax depth (drawn before the shops)
    function drawDistant(t) {
      var col = phase === 'night' ? '#0e1830' : (phase === 'day' ? '#a9c9e9' : '#77679b');
      var roofC = phase === 'night' ? '#080f1e' : (phase === 'day' ? '#8fb0d8' : '#5d527c');
      for (var k = 0; k < 8; k++) {
        var bx = k * 170 - 40 + Math.sin(t * 0.1 + k * 1.3) * 2;
        var bw = 130, bh = 60 + ((k * 53) % 40);
        var by = 200 - bh;
        ctx.fillStyle = col; ctx.fillRect(Math.round(bx), by, bw, bh);
        ctx.fillStyle = roofC; ctx.fillRect(Math.round(bx) - 6, by - 14, bw + 12, 14);
        if (PHASES[phase].lamps && k % 2 === 0) {
          ctx.fillStyle = 'rgba(255,233,163,0.45)';
          ctx.fillRect(Math.round(bx) + 20 + (k * 37) % 60, by + 22, 8, 10);
        }
      }
    }

    function phaseOf(p) { return PHASES[p] ? p : 'day'; }

    function slotIndexFor(buildingSlug) {
      for (var i = 0; i < buildings.length; i++) if (buildings[i].slug === buildingSlug) return i;
      return Math.floor(buildings.length / 2);
    }
    function roomIndexFor(roomId) {
      for (var i = 0; i < rooms.length; i++) if (String(rooms[i].id) === String(roomId)) return i;
      return -1;
    }

    function occupantSpot(occ, t) {
      // deterministic gentle stroll near the occupant's shop, cottage, or the Row
      var seed = hashStr(occ.handle || '?');
      var a = (seed % 628) / 100, b = ((seed >> 8) % 628) / 100, c = ((seed >> 16) % 628) / 100;
      var speed = 0.25 + (seed % 100) / 400;
      var bld = occ.building || 'row';
      var cx, range, yBase;
      if (bld === 'row') {
        cx = W / 2; range = W * 0.42; yBase = GROUND_Y + 40;
      } else if (bld.indexOf('room:') === 0) {
        var ri = roomIndexFor(bld.slice(5));
        if (ri >= 0 && cSlots[ri]) { cx = cSlots[ri].cx; range = 46; yBase = COTTAGE_BASE + 18; }
        else { cx = W / 2; range = W * 0.42; yBase = GROUND_Y + 40; }
      } else {
        var slot = slots[Math.max(0, slotIndexFor(bld))];
        cx = slot.cx; range = slot.w * 0.42; yBase = GROUND_Y + 40;
      }
      var rx = Math.sin(t * speed + a) * range;
      var ry = Math.cos(t * speed * 0.7 + b) * 12;
      var hop = (seed % 2 === 0) ? Math.abs(Math.sin(t * 1.4 + c)) * 3 : 0; // some bob, some don't
      return { x: cx + rx, y: yBase + ry - hop, seed: seed, me: me && occ.handle === me.handle };
    }

    function drawSky(t) {
      var p = PHASES[phase];
      var g = ctx.createLinearGradient(0, 0, 0, SKY_H + 60);
      g.addColorStop(0, p.sky[0]); g.addColorStop(0.55, p.sky[1]); g.addColorStop(1, p.sky[2]);
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, SKY_H + 60);

      // drifting pixel clouds (all phases but night, where they go faint)
      var cloudTop, cloudBot, i;
      if (phase === 'day') { cloudTop = '#ffffff'; cloudBot = '#cfe6f7'; }
      else if (phase === 'dawn') { cloudTop = '#ffe3c8'; cloudBot = '#f0a8b8'; }
      else if (phase === 'dusk') { cloudTop = '#e8a0bf'; cloudBot = '#9a5f8e'; }
      else { cloudTop = '#233a5e'; cloudBot = '#16263f'; }
      for (i = 0; i < CLOUDS.length; i++) {
        var c = CLOUDS[i];
        var cx = ((c.bx + t * c.sp) % (W + 320)) - 160;
        ctx.globalAlpha = phase === 'night' ? 0.5 : 0.92;
        drawCloud(cx, c.y + Math.sin(t * 0.5 + i * 2) * 4, c.s, cloudTop, cloudBot);
      }
      ctx.globalAlpha = 1;

      if (phase === 'night') {
        // stars, twinkling
        var seed = 42;
        for (i = 0; i < 110; i++) {
          seed = (Math.imul(seed, 1103515245) + 12345) >>> 0;
          var sx = (seed % W), sy = (seed >> 9) % 240;
          var tw = 0.5 + 0.5 * Math.sin(t * 2 + i);
          ctx.fillStyle = 'rgba(255,255,255,' + (0.25 + 0.55 * tw).toFixed(2) + ')';
          ctx.fillRect(sx, sy, 2, 2);
        }
        // a few bright stars with cross sparkle
        for (i = 0; i < 8; i++) {
          seed = (Math.imul(seed, 1103515245) + 12345) >>> 0;
          var bx2 = (seed % (W - 40)) + 20, by2 = (seed >> 7) % 200 + 10;
          var sp = 0.5 + 0.5 * Math.sin(t * 3 + i * 1.7);
          ctx.fillStyle = 'rgba(255,255,255,' + (0.5 + 0.5 * sp).toFixed(2) + ')';
          ctx.fillRect(bx2 - 1, by2 - 4, 3, 9); ctx.fillRect(bx2 - 4, by2 - 1, 9, 3);
        }
        // proper crescent moon with halo
        radialGlow(1150, 62, 80, 'rgba(244,241,216,0.22)', 'rgba(244,241,216,0)');
        pxCrescent(1150, 62, 20, '#f4f1d8', 8);
      } else if (phase === 'dawn' || phase === 'dusk') {
        // low sun, warm and huge
        var sunX = phase === 'dawn' ? 210 : 1070, sunC = phase === 'dawn' ? '#fff3c4' : '#ffb26b';
        radialGlow(sunX, 232, 130, 'rgba(255,190,120,0.4)', 'rgba(255,190,120,0)');
        pxDisc(sunX, 232, 26, sunC);
        pxDisc(sunX, 232, 18, '#fff8dc');
      } else {
        // day sun with soft halo
        radialGlow(1150, 72, 110, 'rgba(255,248,220,0.45)', 'rgba(255,248,220,0)');
        pxDisc(1150, 72, 24, '#ffedb0');
        pxDisc(1150, 72, 17, '#fff8dc');
        // birds
        ctx.fillStyle = '#3a4a63';
        for (i = 0; i < BIRDS.length; i++) {
          var b = BIRDS[i];
          var px2 = ((b.bx + t * b.sp) % (W + 200)) - 100;
          var py2 = b.y + Math.sin(t * 1.2 + b.ph) * 8;
          var flap = ((t * 5 + b.ph) | 0) % 2 === 0;
          if (flap) { ctx.fillRect(px2 - 5, py2, 5, 2); ctx.fillRect(px2, py2, 5, 2); }
          else { ctx.fillRect(px2 - 4, py2 - 3, 3, 5); ctx.fillRect(px2 + 1, py2 - 3, 3, 5); }
        }
      }
    }

    function drawShop(i, b, t) {
      var slot = slots[i], paint = paintFor(b.slug);
      var x = slot.x, w = slot.w;
      var wallTop = 152 + (i % 3) * 12;    // slight height variety
      var night = phase === 'night';
      var kind = shopKind(b.slug);
      var sig = String(signals[b.slug] || '');

      // wall — textured per shop kind, with a real roofline above
      paintWall(wallStyle(b.slug), x, wallTop, GROUND_Y, w, paint);
      drawRoof(i, x, w, wallTop, paint);

      // awning: striped or solid-with-scallops, alternating per shop
      var ay = wallTop + 56;
      if (i % 2 === 0) {
        for (var s = 0; s < Math.floor(w / 14); s++) {
          ctx.fillStyle = s % 2 ? paint.awn : paint.trim;
          ctx.fillRect(x + s * 14, ay, 14, 18);
        }
        ctx.fillStyle = paint.trim; ctx.fillRect(x, ay + 18, w, 3);
      } else {
        ctx.fillStyle = paint.awn; ctx.fillRect(x, ay, w, 14);
        ctx.fillStyle = paint.trim;
        for (var sc2 = 0; sc2 < w; sc2 += 12) ctx.fillRect(x + sc2, ay + 14, 6, 6);
        ctx.fillStyle = 'rgba(0,0,0,0.2)'; ctx.fillRect(x, ay + 12, w, 2);
      }

      // name sign: carved board with brass rules
      ctx.fillStyle = '#241a12'; ctx.fillRect(x + 6, wallTop + 8, w - 12, 34);
      ctx.fillStyle = '#e8a93d';
      ctx.fillRect(x + 6, wallTop + 8, w - 12, 2); ctx.fillRect(x + 6, wallTop + 40, w - 12, 2);
      ctx.fillStyle = '#f5e9c8';
      ctx.font = 'bold 13px monospace'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      var name = String(b.name || b.slug || 'Shop');
      if (name.length > 16) name = name.slice(0, 15) + '…';
      ctx.fillText(name, x + w / 2, wallTop + 25);

      // windows — framed, with sills; shutters on library + workshop;
      // glow when lamps are on (bounty shows its board instead;
      // petshop shows its aquarium instead)
      var winOn = PHASES[phase].lamps;
      var wy = ay + 34;
      if (kind !== 'bounty' && kind !== 'petshop') {
        for (var wx = x + 12; wx + 32 <= x + w - 8; wx += 42) {
          ctx.fillStyle = '#241a12'; ctx.fillRect(wx - 3, wy - 3, 38, 56);   // frame
          ctx.fillStyle = winOn ? '#ffe9a3' : '#0e2233';
          ctx.fillRect(wx, wy, 32, 50);                                     // glass
          if (winOn) {
            ctx.fillStyle = '#fff6d8'; ctx.fillRect(wx + 4, wy + 4, 24, 14); // sky glint
            ctx.fillStyle = paint.trim;
            ctx.fillRect(wx + 14, wy, 4, 50); ctx.fillRect(wx, wy + 23, 32, 4);
          } else {
            ctx.fillStyle = 'rgba(120,160,200,0.25)'; ctx.fillRect(wx + 4, wy + 4, 24, 12);
            ctx.fillStyle = '#1b2430';
            ctx.fillRect(wx + 14, wy, 4, 50); ctx.fillRect(wx, wy + 23, 32, 4);
          }
          ctx.fillStyle = '#4a3826'; ctx.fillRect(wx - 4, wy + 53, 40, 5);   // sill
          if (b.slug === 'library' || b.slug === 'workshop') {              // shutters
            ctx.fillStyle = '#5a6e46';
            ctx.fillRect(wx - 11, wy - 3, 8, 56); ctx.fillRect(wx + 35, wy - 3, 8, 56);
            ctx.fillStyle = '#42522f';
            for (var sh2 = wy + 2; sh2 < wy + 50; sh2 += 8) {
              ctx.fillRect(wx - 11, sh2, 8, 2); ctx.fillRect(wx + 35, sh2, 8, 2);
            }
          }
        }
      }

      // flower boxes under the library windows
      if (b.slug === 'library') {
        for (var fx = x + 12; fx + 32 <= x + w - 8; fx += 84) {
          ctx.fillStyle = '#5a3d24'; ctx.fillRect(fx - 2, wy + 58, 36, 10);
          ctx.fillStyle = '#3f6e2f';
          for (var fl = 0; fl < 5; fl++) ctx.fillRect(fx + fl * 7, wy + 58 - 8 - (fl % 2) * 3, 5, 10);
          ctx.fillStyle = '#e86a8a';
          for (var fb = 0; fb < 4; fb++) ctx.fillRect(fx + 2 + fb * 9, wy + 58 - 11 - (fb % 2) * 3, 4, 4);
        }
      }

      // door: framed, recessed panels, brass knob, stone step
      var dw = 34, dx0 = x + w / 2 - dw / 2;
      ctx.fillStyle = '#241a12'; ctx.fillRect(dx0 - 4, GROUND_Y - 64, dw + 8, 64);
      ctx.fillStyle = '#1d1410'; ctx.fillRect(dx0, GROUND_Y - 60, dw, 60);
      ctx.fillStyle = '#2e2018';
      ctx.fillRect(dx0 + 6, GROUND_Y - 54, dw - 12, 22);
      ctx.fillRect(dx0 + 6, GROUND_Y - 28, dw - 12, 22);
      ctx.fillStyle = '#e8a93d'; ctx.fillRect(dx0 + dw - 10, GROUND_Y - 34, 4, 4);
      if (winOn) { ctx.fillStyle = 'rgba(255,233,163,0.9)'; ctx.fillRect(dx0, GROUND_Y - 60, dw, 5); }
      ctx.fillStyle = '#5b5e70'; ctx.fillRect(dx0 - 8, GROUND_Y, dw + 16, 6);
      // woven doormat on the cobbles
      ctx.fillStyle = paint.trim; ctx.fillRect(dx0 - 6, GROUND_Y + 8, dw + 12, 8);
      ctx.fillStyle = 'rgba(0,0,0,0.28)';
      for (var dm = 0; dm < dw + 12; dm += 6) ctx.fillRect(dx0 - 6 + dm, GROUND_Y + 8, 2, 8);
      // OPEN sign hanging on the door when the lamps are lit
      if (winOn) {
        var osy = GROUND_Y - 46;
        ctx.fillStyle = '#14100c'; ctx.fillRect(dx0 + dw / 2 - 1, GROUND_Y - 60, 2, 14);
        ctx.fillStyle = '#2b1d12'; ctx.fillRect(dx0 + dw / 2 - 14, osy, 28, 13);
        ctx.fillStyle = '#e8a93d'; ctx.fillRect(dx0 + dw / 2 - 14, osy, 28, 2);
        ctx.fillStyle = '#ffd23f'; ctx.font = '8px monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText('OPEN', dx0 + dw / 2, osy + 8);
      }

      // crates + barrel beside the workshop / bounty doors
      if (b.slug === 'workshop' || b.slug === 'bounty') {
        var crx = dx0 - 32;
        ctx.fillStyle = '#8a6d4f'; ctx.fillRect(crx, GROUND_Y - 24, 24, 24);
        ctx.fillStyle = '#5f4a33';
        ctx.fillRect(crx, GROUND_Y - 24, 24, 3); ctx.fillRect(crx, GROUND_Y - 3, 24, 3);
        ctx.fillRect(crx, GROUND_Y - 24, 3, 24); ctx.fillRect(crx + 21, GROUND_Y - 24, 3, 24);
        ctx.fillRect(crx, GROUND_Y - 14, 24, 3);
        var brx = dx0 + dw + 8;
        ctx.fillStyle = '#6b5138'; ctx.fillRect(brx, GROUND_Y - 28, 20, 28);
        ctx.fillStyle = '#4a3826';
        ctx.fillRect(brx, GROUND_Y - 28, 20, 3); ctx.fillRect(brx, GROUND_Y - 3, 20, 3);
        ctx.fillStyle = '#2b2f38';
        ctx.fillRect(brx, GROUND_Y - 20, 20, 2); ctx.fillRect(brx, GROUND_Y - 10, 20, 2);
      }

      // blade signs for the radio station + pet shop (drawn on top, after all shops)
      if (i === 0 || i === 4) overlays.push({ x: x, w: w, wallTop: wallTop, emoji: shopEmoji(b.slug), name: String(b.name || b.slug) });

      // warm light spilling from windows + door onto the cobbles (drawn by drawGlow)
      if (winOn) {
        glowSpots.push({ x: x + w / 2, y: GROUND_Y + 30, rx: w * 0.52, ry: 22, a: 0.26 });
        glowSpots.push({ x: x + w / 2, y: GROUND_Y + 54, rx: 36, ry: 13, a: 0.30 });
      }

      // live signal marquee at the base of the facade
      ctx.fillStyle = '#0d1420'; ctx.fillRect(x, GROUND_Y - 26, w, 22);
      ctx.fillStyle = '#7df9ff'; ctx.font = '11px monospace'; ctx.textAlign = 'center';
      var txt = sig.length > 26 ? sig.slice(0, 25) + '…' : sig;
      ctx.fillText(txt || '·', x + w / 2, GROUND_Y - 15);

      // special bits per shop kind
      if (kind === 'radio') {
        // blinking tower on the roof
        var tx = x + w - 22, ty = wallTop - 64;
        ctx.fillStyle = paint.trim; ctx.fillRect(tx, ty, 8, 64);
        ctx.fillRect(tx - 6, ty + 20, 20, 3); ctx.fillRect(tx - 4, ty + 40, 16, 3);
        var blink = (t * 2 | 0) % 2 === 0;
        ctx.fillStyle = blink ? '#ff4d5e' : '#7a1f2b';
        ctx.fillRect(tx - 2, ty - 8, 12, 8);
        if (blink) { // signal rings
          ctx.strokeStyle = 'rgba(255,77,94,0.6)'; ctx.lineWidth = 2;
          ctx.strokeRect(tx - 10, ty - 16, 28, 24);
        }
      } else if (kind === 'petshop') {
        // aquarium window — pets in the window, animated waves, bobbing pets
        var wx = x + 10, wy = ay + 34, ww = w - 20, wh = 60;
        ctx.fillStyle = '#0b3a55'; ctx.fillRect(wx, wy, ww, wh);
        var waves = 3, q, px2;
        for (q = 0; q < waves; q++) {
          ctx.fillStyle = q % 2 ? '#2fd4ff' : '#12b5a5';
          for (px2 = 0; px2 < ww; px2 += 8) {
            var wyy = wy + 8 + q * 16 + Math.sin(t * 2.2 + px2 / 18 + q) * 3;
            ctx.fillRect(wx + px2, wyy, 8, 3);
          }
        }
        // two bobbing pets (simple fish glyphs)
        for (var f = 0; f < 2; f++) {
          var fx = wx + 20 + f * (ww / 2 - 10) + Math.sin(t * 1.3 + f * 2.4) * 8;
          var fy = wy + 20 + f * 16 + Math.cos(t * 2 + f) * 4;
          ctx.fillStyle = f ? '#ff6fae' : '#ffd23f';
          ctx.fillRect(fx, fy, 12, 6); ctx.fillRect(fx - 4, fy + 1, 4, 4);
          ctx.fillStyle = '#1b2430'; ctx.fillRect(fx + 8, fy + 1, 2, 2);
        }
        if (night) { // bioluminescent dots
          for (var d = 0; d < 8; d++) {
            var dx = wx + ((d * 37 + 11) % ww), dy = wy + ((d * 53 + 7) % wh);
            var glow = 0.4 + 0.6 * Math.abs(Math.sin(t * 3 + d));
            ctx.fillStyle = 'rgba(125,249,255,' + glow.toFixed(2) + ')';
            ctx.fillRect(dx, dy, 2, 2);
          }
        }
      } else if (kind === 'arena') {
        // "NOW PLAYING" board above the door
        ctx.fillStyle = '#101418'; ctx.fillRect(x + w / 2 - 46, ay - 26, 92, 20);
        var on = (t * 1.5 | 0) % 2 === 0;
        ctx.fillStyle = on ? '#ffe95e' : '#8a7430'; ctx.font = 'bold 9px monospace';
        ctx.fillText('NOW PLAYING', x + w / 2, ay - 16);
      } else if (kind === 'bounty') {
        // pinned-notes board: cork + pinned notes
        var bx0 = x + 10, by0 = ay + 34, bw = w - 20, bh = 58;
        ctx.fillStyle = paint.trim; ctx.fillRect(bx0 - 3, by0 - 3, bw + 6, bh + 6);
        ctx.fillStyle = '#b08d57'; ctx.fillRect(bx0, by0, bw, bh);
        var noteCols = ['#fff3a0', '#ffd9e8', '#d9f2ff', '#e2ffd9'];
        for (var n = 0; n < 6; n++) {
          var nx = bx0 + 8 + (n % 3) * ((bw - 16) / 3);
          var ny = by0 + 7 + ((n / 3) | 0) * 26;
          var nw = (bw - 16) / 3 - 6;
          ctx.fillStyle = noteCols[n % 4]; ctx.fillRect(nx, ny, nw, 20);
          ctx.fillStyle = '#c2452d'; ctx.fillRect(nx + nw / 2 - 2, ny - 2, 4, 4); // pin
          ctx.fillStyle = 'rgba(0,0,0,0.35)';
          ctx.fillRect(nx + 4, ny + 6, nw - 8, 2);
          ctx.fillRect(nx + 4, ny + 11, nw - 12, 2);
        }
      } else if (kind === 'openmic') {
        // stage venue: spotlight cone + low stage in front of the door
        var cxm = x + w / 2;
        ctx.fillStyle = night ? 'rgba(255,240,180,0.14)' : 'rgba(255,240,180,0.07)';
        ctx.beginPath();
        ctx.moveTo(cxm - 8, wallTop + 40); ctx.lineTo(cxm + 8, wallTop + 40);
        ctx.lineTo(cxm + 55, GROUND_Y); ctx.lineTo(cxm - 55, GROUND_Y);
        ctx.closePath(); ctx.fill();
        // stage platform
        ctx.fillStyle = '#8a6d4f'; ctx.fillRect(cxm - 48, GROUND_Y - 8, 96, 14);
        ctx.fillStyle = '#5f4a33'; ctx.fillRect(cxm - 48, GROUND_Y - 8, 96, 3);
        // microphone stand on the stage
        ctx.fillStyle = '#1c2430'; ctx.fillRect(cxm - 1, GROUND_Y - 34, 3, 26);
        ctx.fillRect(cxm - 8, GROUND_Y - 40, 16, 7);
        // glowing LIVE banner when the signal says LIVE
        if (/live/i.test(sig)) {
          ctx.fillStyle = 'rgba(255,60,60,0.30)';
          ctx.fillRect(cxm - 44, GROUND_Y - 52, 88, 26);
          ctx.fillStyle = '#3d0d12'; ctx.fillRect(cxm - 40, GROUND_Y - 48, 80, 22);
          var pulse = 0.7 + 0.3 * Math.sin(t * 4);
          ctx.fillStyle = 'rgba(255,91,91,' + pulse.toFixed(2) + ')';
          ctx.font = 'bold 12px monospace';
          ctx.fillText('● LIVE', cxm, GROUND_Y - 37);
        }
      }
    }

    function drawStreet(t) {
      var p = PHASES[phase];
      // cobblestone sidewalk: dark gaps, varied stones, top highlight
      ctx.fillStyle = '#434656'; ctx.fillRect(0, GROUND_Y, W, 92);
      var cobH = 15, cobW = 26, brow, cx0;
      for (brow = 0; brow * cobH < 92; brow++) {
        var y0 = GROUND_Y + brow * cobH;
        var off = (brow % 2) * cobW / 2;
        for (cx0 = -cobW; cx0 < W + cobW; cx0 += cobW) {
          var hx = hashStr(cx0 + ':' + brow);
          var v = 132 + (hx % 26) - 13;
          ctx.fillStyle = 'rgb(' + v + ',' + v + ',' + (v + 10) + ')';
          ctx.fillRect(cx0 + off + 1, y0 + 1, cobW - 2, cobH - 2);
          ctx.fillStyle = 'rgba(255,255,255,0.10)';
          ctx.fillRect(cx0 + off + 1, y0 + 1, cobW - 2, 2);
          ctx.fillStyle = 'rgba(0,0,0,0.16)';
          ctx.fillRect(cx0 + off + 1, y0 + cobH - 3, cobW - 2, 2);
        }
      }
      // phase tint unifies the street lighting
      if (phase === 'night') { ctx.fillStyle = 'rgba(8,12,26,0.42)'; ctx.fillRect(0, GROUND_Y, W, 92); }
      else if (phase === 'dusk') { ctx.fillStyle = 'rgba(52,24,54,0.22)'; ctx.fillRect(0, GROUND_Y, W, 92); }
      else if (phase === 'dawn') { ctx.fillStyle = 'rgba(255,170,140,0.10)'; ctx.fillRect(0, GROUND_Y, W, 92); }
      // curb + boardwalk lane: weathered planks (the Row is a boardwalk, not a road)
      ctx.fillStyle = '#5b5e70'; ctx.fillRect(0, GROUND_Y + 92, W, 4);
      ctx.fillStyle = '#3a2c1e'; ctx.fillRect(0, GROUND_Y + 96, W, H - GROUND_Y - 96);
      for (var py = GROUND_Y + 96; py < H; py += 16) {
        var pv = 96 + (hashStr('bw' + py) % 22);
        ctx.fillStyle = 'rgb(' + pv + ',' + ((pv * 0.72) | 0) + ',' + ((pv * 0.52) | 0) + ')';
        ctx.fillRect(0, py + 1, W, 14);
        ctx.fillStyle = 'rgba(255,255,255,0.07)'; ctx.fillRect(0, py + 1, W, 2);
        ctx.fillStyle = '#3a2c1e';
        ctx.fillRect(hashStr('j' + py) % W, py + 1, 3, 14);                  // butt joint
        ctx.fillStyle = 'rgba(0,0,0,0.12)';                                  // grain
        for (var gg = 0; gg < 6; gg++) {
          var gx = hashStr('g' + py + ':' + gg) % W;
          ctx.fillRect(gx, py + 5 + (gg % 2) * 4, 26, 2);
        }
      }
      if (phase === 'night') {
        ctx.fillStyle = 'rgba(8,12,26,0.35)'; ctx.fillRect(0, GROUND_Y + 96, W, H - GROUND_Y - 96);
        // puddles catching the lamplight
        var pud = [[300, 520], [760, 532], [1100, 518]];
        for (var pi = 0; pi < pud.length; pi++) {
          var ex = pud[pi][0], ey = pud[pi][1], er = 26 + (pi * 7) % 14;
          ctx.save(); ctx.translate(ex, ey); ctx.scale(1, 0.32);
          var pg = ctx.createRadialGradient(0, 0, 2, 0, 0, er);
          pg.addColorStop(0, 'rgba(255,190,90,0.16)');
          pg.addColorStop(0.7, 'rgba(150,180,220,0.10)');
          pg.addColorStop(1, 'rgba(150,180,220,0)');
          ctx.fillStyle = pg; ctx.fillRect(-er, -er, er * 2, er * 2);
          ctx.restore();
        }
      } else if (phase === 'dusk') {
        ctx.fillStyle = 'rgba(52,24,54,0.18)'; ctx.fillRect(0, GROUND_Y + 96, W, H - GROUND_Y - 96);
      }
      ctx.fillStyle = 'rgba(255,255,255,0.22)';
      for (var x = 12; x < W; x += 72) ctx.fillRect(x, GROUND_Y + 138, 36, 5);
      // lamp posts: iron post, curved arm, hanging lantern, cone + pool when lit
      for (var i = 0; i < 4; i++) {
        var lx = 90 + i * 360;
        var lit = p.lamps;
        ctx.fillStyle = '#14100c';
        ctx.fillRect(lx, GROUND_Y - 118, 8, 118);          // post
        ctx.fillRect(lx - 4, GROUND_Y - 4, 16, 4);          // foot
        ctx.fillRect(lx, GROUND_Y - 118, 30, 6);            // arm
        ctx.fillRect(lx + 26, GROUND_Y - 118, 4, 12);       // hanger
        ctx.fillRect(lx + 22, GROUND_Y - 106, 12, 4);       // lantern cap
        ctx.fillStyle = lit ? '#ffe9a3' : '#39424f';
        ctx.fillRect(lx + 23, GROUND_Y - 102, 10, 12);      // lantern glass
        ctx.fillStyle = '#14100c';
        ctx.fillRect(lx + 22, GROUND_Y - 90, 12, 3);        // lantern base
        if (lit) {
          ctx.fillStyle = '#fff6d8';
          ctx.fillRect(lx + 26, GROUND_Y - 100, 4, 8);      // hot core
          radialGlow(lx + 28, GROUND_Y - 96, 46, 'rgba(255,220,140,0.5)', 'rgba(255,220,140,0)');
          // light cone to the ground
          ctx.fillStyle = 'rgba(255,220,140,0.10)';
          ctx.beginPath();
          ctx.moveTo(lx + 28, GROUND_Y - 90); ctx.lineTo(lx + 2, GROUND_Y + 92);
          ctx.lineTo(lx + 54, GROUND_Y + 92); ctx.closePath(); ctx.fill();
          glowSpots.push({ x: lx + 28, y: GROUND_Y + 62, rx: 66, ry: 20, a: 0.30 });
        }
      }
      // plaques: three small stones embedded in the sidewalk
      for (var k = 0; k < PLAQUES.length; k++) {
        var px = 340 + k * 320, py = GROUND_Y + 40;
        ctx.fillStyle = '#5b6470'; ctx.fillRect(px - 12, py - 8, 24, 16);
        ctx.fillStyle = '#39404a'; ctx.fillRect(px - 12, py - 8, 24, 4);
        ctx.fillStyle = '#e8eef5'; ctx.font = '10px monospace'; ctx.textAlign = 'center';
        ctx.fillText('✦', px, py + 4);
        PLAQUES[k]._x = px; PLAQUES[k]._y = py;
      }
    }

    /* ---- particles: chimney smoke, fireflies, dust motes ---- */
    function drawParticles(t, dt) {
      var lamps = PHASES[phase].lamps;
      // chimney smoke from three shops
      var emitIdx = [0, 3, 5], e, si;
      for (e = 0; e < emitIdx.length; e++) {
        si = emitIdx[e];
        if (si >= slots.length) continue;
        if (Math.random() < dt * 5 && puffs.length < 70) {
          var ct = chimneyTop(si);
          puffs.push({
            x: ct.x,
            y: ct.y - 2,
            age: 0, seed: Math.random() * 10,
          });
        }
      }
      var smokeC = phase === 'night' ? '#3a4658' : (phase === 'day' ? '#dfe3ea' : '#c9b8c4');
      for (var i = puffs.length - 1; i >= 0; i--) {
        var p = puffs[i];
        p.age += dt;
        if (p.age > 4.5) { puffs.splice(i, 1); continue; }
        var px = p.x + p.age * 15 + Math.sin(p.age * 3 + p.seed) * 7;
        var py = p.y - p.age * 24;
        var s = 4 + p.age * 2.6;
        ctx.globalAlpha = 0.40 * (1 - p.age / 4.5);
        ctx.fillStyle = smokeC;
        ctx.fillRect(Math.round(px - s / 2), Math.round(py - s / 2), Math.round(s), Math.round(s));
        ctx.fillRect(Math.round(px - s / 4), Math.round(py - s / 2 - 3), Math.round(s / 2), Math.round(s / 2));
      }
      ctx.globalAlpha = 1;

      var f, fx, fy, tw2;
      if (lamps) {
        // fireflies wander over the street
        for (f = 0; f < flies.length; f++) {
          var fl = flies[f];
          fx = ((fl.seed * 137) % W + W) % W + Math.sin(t * 0.9 + fl.seed) * 42;
          fy = GROUND_Y - 30 - ((fl.seed * 89) % 190) + Math.cos(t * 1.3 + fl.seed * 2) * 20;
          tw2 = 0.3 + 0.7 * Math.abs(Math.sin(t * 2.1 + fl.seed * 3));
          ctx.globalAlpha = tw2 * 0.28;
          ctx.fillStyle = '#e8ff9e';
          ctx.fillRect(Math.round(fx - 3), Math.round(fy - 3), 9, 9);
          ctx.globalAlpha = tw2;
          ctx.fillRect(Math.round(fx - 1), Math.round(fy - 1), 3, 3);
        }
      } else {
        // dust motes drifting in the daylight
        for (f = 0; f < motes.length; f++) {
          var mo = motes[f];
          var mx = (((mo.seed * 211) % (W + 120)) - t * 6) % (W + 120);
          if (mx < 0) mx += W + 120;
          mx -= 60;
          var my = 120 + ((mo.seed * 53) % 200) + Math.sin(t * 0.7 + mo.seed) * 14;
          ctx.globalAlpha = 0.16 + 0.10 * Math.sin(t * 1.5 + mo.seed * 2);
          ctx.fillStyle = '#fff8dc';
          ctx.fillRect(Math.round(mx), Math.round(my), 2, 2);
        }
      }
      ctx.globalAlpha = 1;
    }

    function drawCottages(t) {
      var n = Math.min(rooms.length, COTTAGE_MAX);
      for (var i = 0; i < n; i++) {
        var room = rooms[i], s = cSlots[i];
        var paint = COTTAGE_PAINT[hashStr(room.id) % COTTAGE_PAINT.length];
        var x = s.x, base = COTTAGE_BASE;
        // body
        ctx.fillStyle = paint.wall; ctx.fillRect(x, base - 52, COTTAGE_W, 52);
        // plank lines
        ctx.fillStyle = paint.trim;
        for (var py = base - 44; py < base - 6; py += 12) ctx.fillRect(x, py, COTTAGE_W, 2);
        // stepped pitched roof
        ctx.fillStyle = paint.roof;
        ctx.fillRect(x + 4, base - 62, COTTAGE_W - 8, 10);
        ctx.fillRect(x + 14, base - 70, COTTAGE_W - 28, 8);
        ctx.fillRect(x + 26, base - 76, COTTAGE_W - 52, 6);
        // name board
        var nm = String(room.name || 'workroom');
        if (nm.length > 10) nm = nm.slice(0, 9) + '…';
        ctx.fillStyle = '#241a12'; ctx.fillRect(x + 8, base - 50, 58, 14);
        ctx.fillStyle = '#f5e9c8'; ctx.font = '9px monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(nm, x + 37, base - 43);
        // window — glows when lamps are on
        ctx.fillStyle = PHASES[phase].lamps ? '#ffe9a3' : '#0e2233';
        ctx.fillRect(x + 72, base - 46, 18, 14);
        // door
        var dw2 = 24;
        ctx.fillStyle = '#1d1410'; ctx.fillRect(s.cx - dw2 / 2, base - 36, dw2, 36);
        if (PHASES[phase].lamps) {
          ctx.fillStyle = 'rgba(255,233,163,0.85)';
          ctx.fillRect(s.cx - dw2 / 2, base - 36, dw2, 5);
        }
        room._x = x; room._y = base - 76; room._w = COTTAGE_W; room._h = 76;
      }
      if (rooms.length > COTTAGE_MAX) {
        ctx.fillStyle = 'rgba(13,20,32,0.85)'; ctx.fillRect(W - 210, COTTAGE_BASE - 90, 202, 24);
        ctx.fillStyle = '#7df9ff'; ctx.font = '12px monospace'; ctx.textAlign = 'center';
        ctx.fillText('+' + (rooms.length - COTTAGE_MAX) + ' more cottages', W - 109, COTTAGE_BASE - 74);
      }
    }

    function drawOccupants(t) {
      var drawn = Math.min(occupants.length, 40);
      for (var i = 0; i < drawn; i++) {
        var occ = occupants[i], spot = occupantSpot(occ, t);
        var size = occ.me ? 52 : 44;
        var bob = Math.sin(t * 2 + i * 1.7) * 2;   // idle bob
        if (i === hoverIdx) {
          radialGlow(spot.x, spot.y - size / 2, size, 'rgba(255,233,163,0.28)', 'rgba(255,233,163,0)');
          ctx.strokeStyle = '#ffe9a3'; ctx.lineWidth = 2;
          ctx.strokeRect(spot.x - size / 2 - 4, spot.y - size - 4 + bob, size + 8, size + 8);
        }
        drawAvatar(ctx, occ.avatar, spot.x - size / 2, spot.y - size + bob, size, performance.now());
        // soft stepped shadow
        ctx.fillStyle = 'rgba(0,0,0,0.28)';
        ctx.fillRect(spot.x - 12, spot.y - 2, 24, 3);
        ctx.fillStyle = 'rgba(0,0,0,0.14)';
        ctx.fillRect(spot.x - 17, spot.y - 1, 34, 2);
        // nameplate with brass rule; verified passports get a green check
        var nm = String(occ.handle || '?') + (occ.me ? ' ★' : '');
        ctx.font = '9px monospace';
        var nw = ctx.measureText(nm).width + 10;
        var ny = spot.y - size + bob - 16;
        ctx.fillStyle = 'rgba(13,20,32,0.80)';
        ctx.fillRect(spot.x - nw / 2, ny - 7, nw, 14);
        ctx.fillStyle = occ.me ? '#ffd23f' : '#e8a93d';
        ctx.fillRect(spot.x - nw / 2, ny - 7, nw, 2);
        ctx.fillStyle = '#f5e9c8'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(nm, spot.x, ny + 1);
        if (occ.passport && occ.passport.verified) {
          var cx = spot.x + nw / 2 + 3;
          ctx.fillStyle = '#2fd67c'; ctx.fillRect(cx, ny - 5, 10, 10);
          ctx.fillStyle = '#0d1420'; ctx.font = 'bold 8px monospace';
          ctx.fillText('✓', cx + 5, ny + 1);
        }
        occ._x = spot.x; occ._y = spot.y - size / 2; occ._size = size;
      }
      if (occupants.length > 40) {
        ctx.fillStyle = 'rgba(13,20,32,0.85)'; ctx.fillRect(W - 190, GROUND_Y + 8, 182, 24);
        ctx.fillStyle = '#7df9ff'; ctx.font = '12px monospace'; ctx.textAlign = 'center';
        ctx.fillText('+' + (occupants.length - 40) + ' more on the Row', W - 99, GROUND_Y + 24);
      }
    }

    // blade signs + festoon lights, drawn over the shop fronts
    function drawOverlays(t) {
      var lit = PHASES[phase].lamps, k;
      for (k = 0; k < overlays.length; k++) {
        var o = overlays[k];
        var ax = o.x + o.w - 2, ayy = o.wallTop + 84;
        ctx.fillStyle = '#14100c';
        ctx.fillRect(ax, ayy, 30, 4);            // bracket arm
        ctx.fillRect(ax + 26, ayy, 4, 12);       // end support
        ctx.fillRect(ax + 6, ayy + 4, 4, 10);    // hanger
        var sx = ax + 2, sy = ayy + 14, swd = 40, sh = 34;
        ctx.fillStyle = '#2b1d12'; ctx.fillRect(sx, sy, swd, sh);
        ctx.fillStyle = '#e8a93d';
        ctx.fillRect(sx, sy, swd, 3); ctx.fillRect(sx, sy + sh - 3, swd, 3);
        ctx.font = '16px monospace'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillStyle = '#f5e9c8';
        ctx.fillText(o.emoji, sx + swd / 2, sy + sh / 2 - 4);
        ctx.font = '8px monospace';
        ctx.fillText(String(o.name).split(' ')[0].slice(0, 8), sx + swd / 2, sy + sh - 9);
      }
      overlays.length = 0;
      // festoon light strings swagged across each shop front
      for (var i = 0; i < slots.length; i++) {
        var x0 = slots[i].x + 6, x1 = slots[i].x + slots[i].w - 6;
        var y0 = (152 + (i % 3) * 12) - 48;
        var steps = 20, s2;
        ctx.fillStyle = '#14100c';
        for (s2 = 0; s2 <= steps; s2++) {
          var tt = s2 / steps;
          var wx = x0 + (x1 - x0) * tt;
          var wyy = y0 + 16 * 4 * tt * (1 - tt);
          ctx.fillRect(Math.round(wx), Math.round(wyy), 2, 2);
        }
        for (var b2 = 1; b2 < 8; b2++) {
          var tt2 = b2 / 8;
          var bx = x0 + (x1 - x0) * tt2;
          var by = y0 + 16 * 4 * tt2 * (1 - tt2);
          var sway = Math.sin(t * 2.4 + i * 1.7 + b2) * 1.5;
          if (lit) {
            radialGlow(bx + sway, by + 4, 11, 'rgba(255,210,130,0.5)', 'rgba(255,210,130,0)');
            ctx.fillStyle = '#fff3c4';
          } else ctx.fillStyle = '#8a8fa0';
          ctx.fillRect(Math.round(bx + sway) - 1, Math.round(by) + 3, 3, 4);
        }
      }
    }

    var lastT = 0, fadeT = 1, fadeFrom = '#000000';
    function frame(nowMs) {
      if (!running) return;
      var t = (nowMs - startT) / 1000;
      var dt = Math.min(0.1, (t - lastT) || 0.016); lastT = t;
      var newPhase = phaseOf(S.phase);
      if (newPhase !== phase) { fadeFrom = PHASES[phase].sky[0]; phase = newPhase; fadeT = 0; }
      drawSky(t);
      drawDistant(t);        // parallax rooftops behind the shops
      for (var i = 0; i < buildings.length; i++) drawShop(i, buildings[i], t);
      drawStreet(t);
      drawGlow();            // window + lamp light pools on the cobbles
      drawOverlays(t);       // blade signs + festoon lights
      drawCottages(t);
      drawOccupants(t);
      drawParticles(t, dt);  // smoke, fireflies, dust
      if (fadeT < 1) {       // phase-change fade: wash of the OLD sky, dissolving out
        fadeT = Math.min(1, fadeT + dt / 0.7);
        var fch = fadeFrom;
        var fr = parseInt(fch.slice(1, 3), 16), fg = parseInt(fch.slice(3, 5), 16), fb = parseInt(fch.slice(5, 7), 16);
        ctx.fillStyle = 'rgba(' + fr + ',' + fg + ',' + fb + ',' + (0.85 * (1 - fadeT)).toFixed(3) + ')';
        ctx.fillRect(0, 0, W, H);
      }
      rafId = requestAnimationFrame(frame);
    }

    /* ---- passport cards, tooltips, roster ---- */
    function ensureTooltip() {
      if (tooltip) return tooltip;
      tooltip = document.createElement('div');
      tooltip.className = 'row-tip';
      canvas.parentElement.style.position = 'relative';
      canvas.parentElement.appendChild(tooltip);
      return tooltip;
    }
    function placeNameFor(slug) {
      slug = String(slug || 'row');
      if (slug === 'row') return 'the Row';
      if (slug.indexOf('room:') === 0) {
        var id = slug.slice(5);
        for (var i = 0; i < rooms.length; i++) {
          if (String(rooms[i].id) === id) return rooms[i].name || 'a workroom';
        }
        return 'a workroom';
      }
      for (var j = 0; j < buildings.length; j++) {
        if (buildings[j].slug === slug) return buildings[j].name;
      }
      return 'the Row';
    }
    // passport card inner HTML (text only; avatar canvas is drawn separately).
    function passportInnerHTML(occ) {
      var p = occ.passport || {};
      var h = '<div class="rp-head"><b>@' + escapeHtml(occ.handle) + '</b>';
      if (p.verified) h += ' <span class="rp-verified" title="verified identity">✓</span>';
      h += '</div>';
      h += '<div class="rp-tier">' + escapeHtml(p.tier || 'unranked') +
        ' · <b>' + escapeHtml(p.score == null ? '—' : p.score) + '</b> pts</div>';
      var badges = (p.badges || []).slice(0, 4);
      if (badges.length) {
        h += '<div class="rp-badges">';
        for (var i = 0; i < badges.length; i++) {
          var bn = (badges[i] && typeof badges[i] === 'object') ? badges[i].name : badges[i];
          h += '<span class="rp-badge">' + escapeHtml(bn) + '</span>';
        }
        h += '</div>';
      }
      h += '<div class="rp-meta">' + escapeHtml(p.endorsements == null ? 0 : p.endorsements) +
        ' endorsements</div>';
      h += '<div class="rp-where">at ' + escapeHtml(placeNameFor(occ.building)) + '</div>';
      return h;
    }
    function showPassport(occ, rx, ry, pinMs) {
      var tip = ensureTooltip();
      tip.innerHTML = '<div class="row-passport"><canvas class="rp-ava" width="24" height="24"></canvas>' +
        '<div class="rp-body">' + passportInnerHTML(occ) + '</div></div>';
      var cv = tip.querySelector('canvas');
      if (cv && cv.getContext) {
        var c2 = cv.getContext('2d');
        c2.imageSmoothingEnabled = false;
        drawAvatar(c2, occ.avatar, 0, 0, 24, performance.now());
      }
      tip.style.display = 'block';
      tip.style.left = Math.min(rx + 14, Math.max(4, canvas.clientWidth - 300)) + 'px';
      tip.style.top = Math.max(ry - 110, 4) + 'px';
      if (tip._pinT) { clearTimeout(tip._pinT); tip._pinT = 0; }
      if (pinMs) {
        tip._pinT = setTimeout(function () { tip.style.display = 'none'; tip._pinT = 0; }, pinMs);
      }
    }
    function showSimpleTip(occ, rx, ry) {
      var tip = ensureTooltip();
      tip.innerHTML = '<b>@' + escapeHtml(occ.handle) + '</b><br><span>' +
        (occ.me ? 'you, ' : '') + 'hanging out at ' + escapeHtml(placeNameFor(occ.building)) + '</span>';
      tip.style.display = 'block';
      tip.style.left = Math.min(rx + 14, Math.max(4, canvas.clientWidth - 300)) + 'px';
      tip.style.top = Math.max(ry - 56, 4) + 'px';
    }

    // "Who's here" roster — occupants grouped by shop / cottage / the Row.
    function renderRoster() {
      var aside = document.getElementById('row-roster');
      if (!aside) return;
      var groups = buildRosterData(occupants, buildings, rooms);
      var membersInOrder = [];
      var html = '<div class="rr-head"><b>👥 Who\'s here</b>' +
        '<span><span class="rr-count">' + occupants.length + '</span> ' +
        '<button type="button" class="rr-close" id="row-roster-close" aria-label="Close">✕</button></span></div>';
      if (!groups.length) {
        html += '<p class="rr-empty">Nobody\'s checked in yet — be the first.</p>';
      }
      for (var i = 0; i < groups.length; i++) {
        var g = groups[i];
        html += '<div class="rr-group"><div class="rr-gname">' + escapeHtml(g.emoji) + ' ' +
          escapeHtml(g.name) + ' <span>(' + g.members.length + ')</span></div>';
        for (var j = 0; j < g.members.length; j++) {
          var m = g.members[j];
          membersInOrder.push(m);
          html += '<div class="rr-member"><canvas width="24" height="24"></canvas>' +
            '<span>@' + escapeHtml(m.handle) + '</span></div>';
        }
        html += '</div>';
      }
      aside.innerHTML = html;
      var cvs = aside.querySelectorAll('canvas');
      for (var k = 0; k < cvs.length && k < membersInOrder.length; k++) {
        var c2d = cvs[k].getContext('2d');
        if (c2d) {
          c2d.imageSmoothingEnabled = false;
          drawAvatar(c2d, membersInOrder[k].avatar, 0, 0, 24, performance.now());
        }
      }
      var close = document.getElementById('row-roster-close');
      if (close) close.addEventListener('click', function () { aside.hidden = true; });
      var count = document.getElementById('row-roster-count');
      if (count) count.textContent = occupants.length ? '(' + occupants.length + ')' : '';
    }

    /* ---- hover tooltips + click handling ---- */
    function canvasXY(e) {
      var r = canvas.getBoundingClientRect();
      var cx = (e.clientX - r.left) * (W / r.width);
      var cy = (e.clientY - r.top) * (H / r.height);
      return { x: cx, y: cy, rx: e.clientX - r.left, ry: e.clientY - r.top };
    }
    function occupantAt(p) {
      for (var i = 0; i < Math.min(occupants.length, 40); i++) {
        var o = occupants[i];
        if (Math.abs(p.x - o._x) < 30 && Math.abs(p.y - o._y) < 34) return i;
      }
      return -1;
    }
    function cottageAt(p) {
      var n = Math.min(rooms.length, COTTAGE_MAX);
      for (var i = 0; i < n; i++) {
        var r = rooms[i];
        if (r._x != null && p.x >= r._x && p.x <= r._x + r._w && p.y >= r._y && p.y <= r._y + r._h) return i;
      }
      return -1;
    }
    function shopAt(p) {
      for (var i = 0; i < buildings.length; i++) {
        var s = slots[i];
        if (p.x >= s.x && p.x <= s.x + s.w && p.y >= 120 && p.y <= GROUND_Y) return i;
      }
      return -1;
    }
    function goTo(door) {
      if (/^https?:\/\//i.test(door)) window.open(door, '_blank', 'noopener');
      else window.location.href = door;
    }
    canvas.addEventListener('mousemove', function (e) {
      var p = canvasXY(e), found = occupantAt(p);
      hoverIdx = found;
      if (found >= 0) {
        var occ = occupants[found];
        if (occ.passport) showPassport(occ, p.rx, p.ry, 0);
        else showSimpleTip(occ, p.rx, p.ry);
      } else if (tooltip) { tooltip.style.display = 'none'; }
      canvas.style.cursor = (found >= 0 || cottageAt(p) >= 0 || shopAt(p) >= 0) ? 'pointer' : 'default';
    });
    canvas.addEventListener('mouseleave', function () {
      hoverIdx = -1; if (tooltip) tooltip.style.display = 'none';
    });
    canvas.addEventListener('click', function (e) {
      var p = canvasXY(e);
      // occupants: pin their passport card (touch-friendly)
      var oi = occupantAt(p);
      if (oi >= 0) {
        var occ = occupants[oi];
        if (occ.passport) showPassport(occ, p.rx, p.ry, 6000);
        else showSimpleTip(occ, p.rx, p.ry);
        return;
      }
      // plaques
      for (var k = 0; k < PLAQUES.length; k++) {
        var pl = PLAQUES[k];
        if (Math.abs(p.x - pl._x) < 22 && Math.abs(p.y - pl._y) < 18) {
          var tip = ensureTooltip();
          tip.innerHTML = '<b>' + escapeHtml(pl.name) + '</b><br><span>' + escapeHtml(pl.story) + '</span>';
          tip.style.display = 'block';
          tip.style.left = Math.min(p.rx + 14, Math.max(4, canvas.clientWidth - 300)) + 'px';
          tip.style.top = Math.max(p.ry - 70, 4) + 'px';
          setTimeout(function () { tip.style.display = 'none'; }, 6000);
          return;
        }
      }
      // workroom cottages: click -> enter the room
      var ci = cottageAt(p);
      if (ci >= 0) { goTo(rooms[ci].door || '/workroom'); return; }
      // shops: click a shopfront -> walk in
      var si = shopAt(p);
      if (si >= 0) { goTo(buildings[si].door || '/'); return; }
    });

    rafId = requestAnimationFrame(frame);
    return {
      update: function (ns) {
        if (!ns) return;
        S = ns; buildings = ns.buildings || buildings; signals = ns.signals || {};
        rooms = ns.rooms || rooms;
        occupants = ns.occupants || []; me = ns.me || me;
        slots = shopSlots(buildings.length);
        cSlots = cottageSlots(rooms);
        renderRoster();
      },
      stop: function () { running = false; cancelAnimationFrame(rafId); },
      renderRoster: renderRoster,
      plaques: PLAQUES,
    };
  }

  /* ---------------------------------------------------------------
   * Row.start(canvas, opts) — wire everything: scene + presence + checkin
   * opts: {chipsEl, rosterEl} — elements for the checkin chips and the
   * "who's here" roster (defaults: #row-chips, #row-roster).
   * Reads window.ROW_STATE (injected by row.html).
   * --------------------------------------------------------------- */
  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    if (m) return m.content;
    var inp = document.querySelector('input[name="csrf_token"]');
    return inp ? inp.value : '';
  }

  function start(canvas, opts) {
    opts = opts || {};
    var state = window.ROW_STATE || { buildings: [], signals: {}, phase: 'day', occupants: [], rooms: [], me: null };
    var ctrl = drawScene(canvas, state);
    var myBuilding = (state.me && state.me.building) || 'row';

    function postCheckin(building) {
      var body = { building: building, csrf_token: csrfToken() };
      return fetch('/api/row/checkin', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(function (r) { return r.json().catch(function () { return {}; }); })
        .catch(function () { return {}; });
    }

    function refreshPresence() {
      fetch('/api/row/presence', { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) return;
          var ns = window.ROW_STATE || {};
          ns.occupants = d.occupants || [];
          if (d.phase) ns.phase = d.phase;
          window.ROW_STATE = ns;
          ctrl.update(ns);
          renderChips();                        // rooms may have changed
        })
        .catch(function () { /* offline? keep the last frame */ });
    }

    // chips: where are you hanging out?
    function renderChips() {
      var el = opts.chipsEl || document.getElementById('row-chips');
      if (!el) return;
      var st = window.ROW_STATE || {};
      var buildings = st.buildings || [];
      var rooms = st.rooms || [];
      var html = '';
      html += chipHtml('row', '🛣️ The Row itself', myBuilding === 'row');
      for (var i = 0; i < buildings.length; i++) {
        var b = buildings[i];
        html += chipHtml(b.slug, shopEmoji(b.slug) + ' ' + escapeHtml(b.name), myBuilding === b.slug);
      }
      for (var j = 0; j < rooms.length; j++) {
        var r = rooms[j], key = 'room:' + r.id;
        html += chipHtml(key, '🏠 ' + escapeHtml(r.name || 'workroom'), myBuilding === key);
      }
      el.innerHTML = html;
      var btns = el.querySelectorAll('button[data-b]');
      for (var k = 0; k < btns.length; k++) {
        (function (btn) {
          btn.addEventListener('click', function () {
            myBuilding = btn.getAttribute('data-b');
            postCheckin(myBuilding).then(renderChips);
            renderChips();
          });
        })(btns[k]);
      }
    }
    function chipHtml(slug, label, active) {
      return '<button type="button" data-b="' + escapeHtml(slug) + '" class="row-chip' +
        (active ? ' active' : '') + '">' + label + '</button>';
    }

    // roster drawer toggle
    var toggle = document.getElementById('row-roster-toggle');
    var aside = opts.rosterEl || document.getElementById('row-roster');
    if (toggle && aside) {
      toggle.addEventListener('click', function () { aside.hidden = !aside.hidden; });
    }

    renderChips();
    ctrl.renderRoster();
    postCheckin(myBuilding);                    // check in on load (default: the Row itself)
    setInterval(refreshPresence, 30000);        // presence refresh
    setInterval(function () { postCheckin(myBuilding); }, 60000); // heartbeat
    return { controller: ctrl, checkin: postCheckin, refresh: refreshPresence };
  }

  window.Row = {
    drawAvatar: drawAvatar, drawScene: drawScene, start: start,
    PALETTE: PALETTE, cleanCfg: cleanCfg, buildRosterData: buildRosterData,
    escapeHtml: escapeHtml,
  };
})();
