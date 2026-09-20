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
      if (phase === 'night') {
        var seed = 42;
        for (var i = 0; i < 110; i++) {
          seed = (Math.imul(seed, 1103515245) + 12345) >>> 0;
          var sx = (seed % W), sy = (seed >> 9) % 240;
          var tw = 0.5 + 0.5 * Math.sin(t * 2 + i);
          ctx.fillStyle = 'rgba(255,255,255,' + (0.25 + 0.55 * tw).toFixed(2) + ')';
          ctx.fillRect(sx, sy, 2, 2);
        }
        // moon
        ctx.fillStyle = '#f4f1d8'; ctx.fillRect(1150, 36, 34, 34);
        ctx.fillStyle = '#0b1b33'; ctx.fillRect(1160, 30, 24, 24); // crescent bite
      } else if (phase === 'dawn' || phase === 'dusk') {
        ctx.fillStyle = phase === 'dawn' ? '#fff3c4' : '#ffb26b';
        ctx.fillRect(160, 190, 46, 46); // low sun
      } else {
        ctx.fillStyle = '#fff8dc'; ctx.fillRect(1150, 40, 44, 44); // day sun
      }
    }

    function drawShop(i, b, t) {
      var slot = slots[i], paint = paintFor(b.slug);
      var x = slot.x, w = slot.w;
      var wallTop = 152 + (i % 3) * 12;    // slight height variety
      var night = phase === 'night';
      var kind = shopKind(b.slug);
      var sig = String(signals[b.slug] || '');

      // wall
      ctx.fillStyle = paint.wall; ctx.fillRect(x, wallTop, w, GROUND_Y - wallTop);
      // brick/shingle texture lines
      ctx.fillStyle = paint.trim;
      for (var ry = wallTop + 18; ry < GROUND_Y - 10; ry += 22) ctx.fillRect(x, ry, w, 2);

      // awning: striped canopy over the front
      var ay = wallTop + 56;
      for (var s = 0; s < Math.floor(w / 14); s++) {
        ctx.fillStyle = s % 2 ? paint.awn : paint.trim;
        ctx.fillRect(x + s * 14, ay, 14, 18);
      }
      ctx.fillStyle = paint.trim; ctx.fillRect(x, ay + 18, w, 3);

      // name sign (painted board)
      ctx.fillStyle = '#241a12'; ctx.fillRect(x + 6, wallTop + 8, w - 12, 34);
      ctx.fillStyle = '#f5e9c8';
      ctx.font = 'bold 13px monospace'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      var name = String(b.name || b.slug || 'Shop');
      if (name.length > 16) name = name.slice(0, 15) + '…';
      ctx.fillText(name, x + w / 2, wallTop + 25);

      // windows — glow when lamps are on (bounty shows its board instead;
      // petshop shows its aquarium instead)
      var winOn = PHASES[phase].lamps;
      if (kind !== 'bounty' && kind !== 'petshop') {
        ctx.fillStyle = winOn ? '#ffe9a3' : '#0e2233';
        ctx.fillRect(x + 10, ay + 34, w * 0.28, 52);
        ctx.fillRect(x + w - 10 - w * 0.28, ay + 34, w * 0.28, 52);
        if (winOn) { // window cross-frames
          ctx.fillStyle = paint.trim;
          ctx.fillRect(x + 10 + w * 0.14 - 2, ay + 34, 4, 52);
          ctx.fillRect(x + 10, ay + 34 + 24, w * 0.28, 4);
          ctx.fillRect(x + w - 10 - w * 0.14 - 2, ay + 34, 4, 52);
          ctx.fillRect(x + w - 10 - w * 0.28, ay + 34 + 24, w * 0.28, 4);
        }
      }

      // door
      var dw = 34;
      ctx.fillStyle = '#1d1410'; ctx.fillRect(x + w / 2 - dw / 2, GROUND_Y - 58, dw, 58);
      if (winOn) { ctx.fillStyle = 'rgba(255,233,163,0.85)'; ctx.fillRect(x + w / 2 - dw / 2, GROUND_Y - 58, dw, 6); }

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
      // sidewalk strip
      ctx.fillStyle = p.street; ctx.fillRect(0, GROUND_Y, W, 92);
      // curb + road
      ctx.fillStyle = '#2b323b'; ctx.fillRect(0, GROUND_Y + 92, W, 8);
      ctx.fillStyle = '#232a33'; ctx.fillRect(0, GROUND_Y + 100, W, H - GROUND_Y - 100);
      ctx.fillStyle = 'rgba(255,255,255,0.22)';
      for (var x = 12; x < W; x += 72) ctx.fillRect(x, GROUND_Y + 134, 36, 5);
      // lamp posts with glow when lamps are on
      for (var i = 0; i < 4; i++) {
        var lx = 90 + i * 360;
        ctx.fillStyle = '#1c2430'; ctx.fillRect(lx, GROUND_Y - 96, 8, 96);
        ctx.fillRect(lx - 16, GROUND_Y - 104, 40, 8);
        var lit = p.lamps;
        ctx.fillStyle = lit ? '#ffe9a3' : '#3a4450';
        ctx.fillRect(lx + 6, GROUND_Y - 98, 16, 12);
        if (lit) {
          var g = ctx.createRadialGradient(lx + 14, GROUND_Y - 92, 4, lx + 14, GROUND_Y - 92, 70);
          g.addColorStop(0, 'rgba(255,233,163,0.35)'); g.addColorStop(1, 'rgba(255,233,163,0)');
          ctx.fillStyle = g; ctx.fillRect(lx - 60, GROUND_Y - 160, 150, 160);
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
        if (i === hoverIdx) {
          ctx.strokeStyle = '#ffe9a3'; ctx.lineWidth = 2;
          ctx.strokeRect(spot.x - size / 2 - 4, spot.y - size - 4, size + 8, size + 8);
        }
        drawAvatar(ctx, occ.avatar, spot.x - size / 2, spot.y - size, size, performance.now());
        // little shadow
        ctx.fillStyle = 'rgba(0,0,0,0.25)';
        ctx.fillRect(spot.x - 12, spot.y - 2, 24, 4);
        // "me" gets a marker
        if (occ.me) {
          ctx.fillStyle = '#ffe9a3'; ctx.font = 'bold 10px monospace'; ctx.textAlign = 'center';
          ctx.fillText('YOU', spot.x, spot.y - size - 8);
        }
        occ._x = spot.x; occ._y = spot.y - size / 2; occ._size = size;
      }
      if (occupants.length > 40) {
        ctx.fillStyle = 'rgba(13,20,32,0.85)'; ctx.fillRect(W - 190, GROUND_Y + 8, 182, 24);
        ctx.fillStyle = '#7df9ff'; ctx.font = '12px monospace'; ctx.textAlign = 'center';
        ctx.fillText('+' + (occupants.length - 40) + ' more on the Row', W - 99, GROUND_Y + 24);
      }
    }

    function frame(nowMs) {
      if (!running) return;
      var t = (nowMs - startT) / 1000;
      phase = phaseOf(S.phase);
      drawSky(t);
      for (var i = 0; i < buildings.length; i++) drawShop(i, buildings[i], t);
      drawStreet(t);
      drawCottages(t);
      drawOccupants(t);
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
