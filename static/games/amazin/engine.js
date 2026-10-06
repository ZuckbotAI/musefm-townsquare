/* Amazin' Rush - game engine (pure logic, no DOM).
 * Exposes createGame(catalog, opts). UI layer binds it to the DOM.
 * Works in browser (script tag) and node (module.exports) for headless tests.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.AmazinEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {

  'use strict';

  var COLOR_HEX = {
    sage: '#9caf88', navy: '#2b3a55', cream: '#f2e8d5',
    rust: '#b5542d', charcoal: '#36454f'
  };
  var CATEGORIES = ['Tech', 'Kitchen', 'Home', 'Apparel', 'Pets', 'Glow'];

  var CUSTOMER_NAMES = ['Dana', 'Rook', 'Pip', 'Moss', 'June', 'Kip', 'Sable',
    'Wren', 'Bram', 'Luz', 'Otto', 'Nia', 'Sol', 'Ash', 'Remy', 'Cleo',
    'Finn', 'Mira', 'Theo', 'Ivy', 'Jasper', 'Noor', 'Pax', 'Quinn'];
  var QUIRKS = ['In a hurry', 'Very particular', 'Eagle eyes', 'Chill browser',
    'Gift buyer', 'Second-guesser', 'Speed demon', 'Nitpicker',
    'Comparison shopper', 'Decoy hunter'];

  var SHIFT_SECONDS = 180;
  var MAX_TICKETS = 4;
  var MULT_MAX = 5;
  var REFUND_PENALTY = 50;
  var FROZEN_SECONDS = 15;

  function mulberry32(seed) {
    var a = seed >>> 0;
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function pick(rng, arr) { return arr[Math.floor(rng() * arr.length)]; }
  function lerp(a, b, t) { return a + (b - a) * t; }

  /* Build product groups: name -> {name, category, colorways:[sku recs]} */
  function indexCatalog(catalog) {
    var byName = {};
    catalog.forEach(function (it) {
      if (!byName[it.name]) {
        byName[it.name] = { name: it.name, category: it.category, colorways: [] };
      }
      byName[it.name].colorways.push(it);
    });
    var products = Object.keys(byName).map(function (k) { return byName[k]; });
    var bySku = {};
    catalog.forEach(function (it) { bySku[it.sku] = it; });
    return { products: products, bySku: bySku, byName: byName };
  }

  function createGame(catalog, opts) {
    opts = opts || {};
    var rng = opts.rng || Math.random;
    var idx = indexCatalog(catalog);
    var ticketSeq = 0;

    var state = {
      screen: 'title',        // title | playing | shiftOver
      timeLeft: SHIFT_SECONDS,
      elapsed: 0,
      score: 0,
      streak: 0,             // consecutive completed orders
      multiplier: 1,
      bestStreak: 0,
      tickets: [],
      cart: [],              // [{sku, qty}]
      activeTicketId: null,
      selectedSku: null,     // currently configured product sku
      selectedQty: 1,
      modal: null,           // {ticketId, problems:[...]} while returns modal open
      ordersCompleted: 0,
      perfectOrders: 0,
      wrongPicks: 0,
      expiredCount: 0,
      deniedCount: 0,
      petMood: 'idle',
      lastInputAt: 0,
      spawnIn: 1.0,
      shiftDone: false,
      stats: null
    };

    var listeners = [];
    function emit(type, data) {
      listeners.forEach(function (fn) { try { fn(type, data || {}); } catch (e) {} });
    }

    function progress() {
      return Math.min(1, state.elapsed / SHIFT_SECONDS);
    }

    function makeTicket() {
      var p = progress();
      var lineTarget = 1 + Math.floor(rng() * (1 + Math.floor(p * 2.4)));
      lineTarget = Math.min(3, Math.max(1, lineTarget));
      var shuffled = idx.products.slice();
      for (var i = shuffled.length - 1; i > 0; i--) {
        var j = Math.floor(rng() * (i + 1));
        var t = shuffled[i]; shuffled[i] = shuffled[j]; shuffled[j] = t;
      }
      var lines = [];
      for (var k = 0; k < lineTarget && k < shuffled.length; k++) {
        var prod = shuffled[k];
        var cw = pick(rng, prod.colorways);
        var decoys = prod.colorways.filter(function (c) { return c.color !== cw.color; });
        var decoy = decoys.length ? pick(rng, decoys) : cw;
        var qty = (rng() < 0.25 + p * 0.35) ? 2 : 1;
        lines.push({
          sku: cw.sku, product: prod.name, color: cw.color,
          qty: qty, price: cw.price, decoyColor: decoy.color
        });
      }
      var patienceMax = Math.round(lerp(90, 60, p) + rng() * 8);
      var frozen = rng() < (0.12 + 0.16 * p);
      var ticket = {
        id: 't' + (++ticketSeq),
        name: pick(rng, CUSTOMER_NAMES),
        quirk: pick(rng, QUIRKS),
        avatarColor: pick(rng, Object.keys(COLOR_HEX)),
        lines: lines,
        patience: patienceMax,
        patienceMax: patienceMax,
        frozen: frozen,
        frozenT: frozen ? FROZEN_SECONDS : 0,
        swapped: false,
        wrongAttempts: 0,
        bornAt: state.elapsed
      };
      return ticket;
    }

    function lineText(line) {
      var s = line.color + ' ' + line.product;
      if (line.qty > 1) s += ' x' + line.qty;
      s += ', not ' + line.decoyColor;
      return s;
    }

    function getTicket(id) {
      for (var i = 0; i < state.tickets.length; i++) {
        if (state.tickets[i].id === id) return state.tickets[i];
      }
      return null;
    }

    function resetStreak() {
      if (state.streak > 0) {
        state.streak = 0;
        state.multiplier = 1;
        emit('multiplier', { multiplier: 1 });
      }
    }

    function setPetMood(m) {
      if (state.petMood !== m) {
        state.petMood = m;
        emit('petMood', { mood: m });
      }
    }

    function touch() {
      state.lastInputAt = state.elapsed;
      if (state.petMood === 'sleepy') setPetMood('idle');
    }

    /* ---------- public actions ---------- */

    function startShift() {
      state.screen = 'playing';
      state.timeLeft = SHIFT_SECONDS;
      state.elapsed = 0;
      state.score = 0;
      state.streak = 0;
      state.multiplier = 1;
      state.bestStreak = 0;
      state.tickets = [];
      state.cart = [];
      state.activeTicketId = null;
      state.selectedSku = null;
      state.selectedQty = 1;
      state.modal = null;
      state.ordersCompleted = 0;
      state.perfectOrders = 0;
      state.wrongPicks = 0;
      state.expiredCount = 0;
      state.deniedCount = 0;
      state.petMood = 'idle';
      state.lastInputAt = 0;
      state.spawnIn = 1.0;
      state.shiftDone = false;
      state.stats = null;
      emit('shiftStart', {});
    }

    function clockOut() {
      state.screen = 'title';
      state.tickets = [];
      state.modal = null;
      emit('clockOut', {});
    }

    function selectTicket(id) {
      touch();
      if (getTicket(id)) {
        state.activeTicketId = id;
        emit('ticketSelect', { id: id });
        return true;
      }
      return false;
    }

    function selectProduct(sku) {
      touch();
      if (idx.bySku[sku]) {
        state.selectedSku = sku;
        state.selectedQty = 1;
        emit('productSelect', { sku: sku });
        return true;
      }
      return false;
    }

    function selectColor(color) {
      touch();
      var rec = idx.bySku[state.selectedSku];
      if (!rec) return false;
      var prod = idx.byName[rec.name];
      for (var i = 0; i < prod.colorways.length; i++) {
        if (prod.colorways[i].color === color) {
          state.selectedSku = prod.colorways[i].sku;
          emit('productSelect', { sku: state.selectedSku });
          return true;
        }
      }
      return false;
    }

    function setQty(n) {
      touch();
      n = Math.max(1, Math.min(9, Math.round(n) || 1));
      state.selectedQty = n;
      emit('qty', { qty: n });
      return n;
    }

    function addToCart() {
      touch();
      if (!state.selectedSku) return { ok: false, reason: 'no-selection' };
      var rec = idx.bySku[state.selectedSku];
      if (!rec) return { ok: false, reason: 'bad-sku' };
      var line = null;
      for (var i = 0; i < state.cart.length; i++) {
        if (state.cart[i].sku === state.selectedSku) { line = state.cart[i]; break; }
      }
      if (line) line.qty = Math.min(9, line.qty + state.selectedQty);
      else state.cart.push({ sku: state.selectedSku, qty: state.selectedQty });
      emit('cart', { cart: state.cart.slice() });
      return { ok: true };
    }

    function removeFromCart(sku) {
      touch();
      state.cart = state.cart.filter(function (l) { return l.sku !== sku; });
      emit('cart', { cart: state.cart.slice() });
    }

    function clearCart() {
      state.cart = [];
      emit('cart', { cart: [] });
    }

    /* Match cart against a ticket. Returns {complete:true} or {complete:false, problems:[...]} */
    function evaluateTicket(ticket) {
      var problems = [];
      var cartByProduct = {};
      state.cart.forEach(function (cl) {
        var rec = idx.bySku[cl.sku];
        if (!rec) return;
        if (!cartByProduct[rec.name]) cartByProduct[rec.name] = [];
        cartByProduct[rec.name].push({ color: rec.color, qty: cl.qty, sku: cl.sku });
      });
      ticket.lines.forEach(function (line, li) {
        var have = cartByProduct[line.product] || [];
        var exact = 0, wrongColor = null;
        have.forEach(function (h) {
          if (h.color === line.color) exact += h.qty;
          else if (!wrongColor) wrongColor = h;
        });
        if (exact >= line.qty) return; // satisfied
        if (exact > 0) {
          problems.push({ type: 'count', line: line, lineIdx: li, haveQty: exact });
        } else if (wrongColor) {
          problems.push({ type: 'color', line: line, lineIdx: li, gotColor: wrongColor.color });
        } else {
          problems.push({ type: 'missing', line: line, lineIdx: li });
        }
      });
      return { complete: problems.length === 0, problems: problems };
    }

    function placeOrder() {
      touch();
      var ticket = getTicket(state.activeTicketId);
      if (!ticket) return { ok: false, reason: 'no-ticket' };
      if (state.modal) return { ok: false, reason: 'modal-open' };
      if (state.cart.length === 0) return { ok: false, reason: 'empty-cart' };

      var evald = evaluateTicket(ticket);
      if (evald.complete) {
        var base = 0;
        ticket.lines.forEach(function (line) { base += line.price * line.qty; });
        var perfect = ticket.wrongAttempts === 0;
        var mult = state.multiplier;
        var bonus = 1;
        if (perfect) bonus += 0.25;
        if (ticket.frozen) bonus += 0.50; // patient tip: placed the original while they waited
        var earned = Math.round(base * mult * bonus);
        state.score += earned;
        state.ordersCompleted++;
        if (perfect) {
          state.perfectOrders++;
          setPetMood('happy');
        }
        state.streak++;
        state.bestStreak = Math.max(state.bestStreak, state.streak);
        var newMult = Math.min(MULT_MAX, 1 + state.streak);
        if (newMult !== state.multiplier) {
          state.multiplier = newMult;
          emit('multiplier', { multiplier: newMult });
        }
        // remove ticket
        state.tickets = state.tickets.filter(function (t) { return t.id !== ticket.id; });
        if (state.activeTicketId === ticket.id) state.activeTicketId = null;
        clearCart();
        emit('orderComplete', {
          ticketId: ticket.id, earned: earned, perfect: perfect,
          frozenTip: ticket.frozen, multiplier: mult
        });
        return { ok: true, earned: earned, perfect: perfect };
      }

      // wrong pick: streak resets, pet sad, returns modal opens (patience paused)
      state.wrongPicks++;
      ticket.wrongAttempts++;
      resetStreak();
      setPetMood('sad');
      state.modal = { ticketId: ticket.id, problems: evald.problems };
      emit('returnsOpen', { ticketId: ticket.id, problems: evald.problems });
      return { ok: false, reason: 'returns', problems: evald.problems };
    }

    function describeProblem(p) {
      if (p.type === 'color') {
        return 'Wrong color on "' + p.line.product + '": they wanted ' +
          p.line.color + ', you bagged ' + p.gotColor + '.';
      }
      if (p.type === 'count') {
        return 'Wrong count on "' + p.line.color + ' ' + p.line.product + '": they wanted x' +
          p.line.qty + ', you bagged x' + p.haveQty + '.';
      }
      return 'Missing "' + p.line.color + ' ' + p.line.product + ' x' + p.line.qty + '".';
    }

    function returnsChoice(choice) {
      var modal = state.modal;
      if (!modal) return { ok: false, reason: 'no-modal' };
      var ticket = getTicket(modal.ticketId);
      state.modal = null;
      if (!ticket) { emit('returnsResolved', { choice: choice, ticketGone: true }); return { ok: true }; }

      if (choice === 'refund') {
        state.score = Math.max(0, state.score - REFUND_PENALTY);
        ticket.patience = Math.min(ticket.patienceMax, ticket.patience + 20);
        clearCart();
        emit('returnsResolved', { choice: 'refund', ticketId: ticket.id });
      } else if (choice === 'exchange') {
        // one more chance on the same ticket; patience keeps draining (was paused)
        emit('returnsResolved', { choice: 'exchange', ticketId: ticket.id });
      } else if (choice === 'deny') {
        state.deniedCount++;
        state.tickets = state.tickets.filter(function (t) { return t.id !== ticket.id; });
        if (state.activeTicketId === ticket.id) state.activeTicketId = null;
        clearCart();
        resetStreak();
        emit('returnsResolved', { choice: 'deny', ticketId: ticket.id });
      } else {
        return { ok: false, reason: 'bad-choice' };
      }
      touch();
      return { ok: true };
    }

    function expireTicket(ticket) {
      state.expiredCount++;
      resetStreak();
      setPetMood('sad');
      state.tickets = state.tickets.filter(function (t) { return t.id !== ticket.id; });
      if (state.activeTicketId === ticket.id) state.activeTicketId = null;
      emit('ticketExpired', { ticketId: ticket.id, name: ticket.name });
    }

    function endShift() {
      state.shiftDone = true;
      var attempts = state.ordersCompleted + state.wrongPicks + state.expiredCount + state.deniedCount;
      var accuracy = attempts > 0 ? Math.round((state.ordersCompleted / attempts) * 100) : 100;
      state.stats = {
        score: state.score,
        ordersCompleted: state.ordersCompleted,
        perfectOrders: state.perfectOrders,
        accuracy: accuracy,
        bestStreak: state.bestStreak,
        wrongPicks: state.wrongPicks,
        expired: state.expiredCount,
        denied: state.deniedCount
      };
      state.screen = 'shiftOver';
      emit('shiftOver', state.stats);
    }

    /* Advance the simulation by dt seconds. */
    function tick(dt) {
      if (state.screen !== 'playing' || state.shiftDone) return;
      dt = Math.max(0, dt);
      state.elapsed += dt;
      var modalOpen = !!state.modal;

      // shift timer always runs (returns modal only pauses patience)
      if (state.timeLeft > 0) {
        state.timeLeft = Math.max(0, state.timeLeft - dt);
      }

      // spawn new tickets while the clock runs
      if (state.timeLeft > 0 && state.tickets.length < MAX_TICKETS) {
        state.spawnIn -= dt;
        if (state.spawnIn <= 0) {
          var t = makeTicket();
          state.tickets.push(t);
          emit('ticketSpawn', { ticketId: t.id });
          var p = progress();
          state.spawnIn = lerp(14, 8, p) * (0.7 + rng() * 0.6);
        }
      }

      // patience + frozen handling (paused while returns modal open)
      if (!modalOpen) {
        var dead = [];
        state.tickets.forEach(function (tk) {
          var drain = tk.frozen ? 0.5 : 1;
          tk.patience -= dt * drain;
          if (tk.frozen) {
            tk.frozenT -= dt;
            if (tk.frozenT <= 0) {
              // they swap: required colorways change to a decoy
              tk.frozen = false;
              tk.swapped = true;
              tk.lines.forEach(function (line) {
                var prod = idx.byName[line.product];
                var others = prod.colorways.filter(function (c) { return c.color !== line.color; });
                if (others.length) {
                  var nc = pick(rng, others);
                  line.color = nc.color;
                  line.sku = nc.sku;
                  line.price = nc.price;
                }
              });
              emit('ticketSwap', { ticketId: tk.id });
            }
          }
          if (tk.patience <= 0) dead.push(tk);
        });
        dead.forEach(expireTicket);
      }

      // sleepy pet after 20s with no input
      if (state.elapsed - state.lastInputAt > 20 && state.petMood === 'idle') {
        setPetMood('sleepy');
      }

      // shift over when clock hit zero and no active tickets remain
      if (state.timeLeft <= 0 && state.tickets.length === 0 && !state.modal) {
        endShift();
      }
    }

    return {
      state: state,
      products: idx.products,
      bySku: idx.bySku,
      categories: CATEGORIES,
      colorHex: COLOR_HEX,
      lineText: lineText,
      describeProblem: describeProblem,
      on: function (fn) { listeners.push(fn); },
      touch: touch,
      setPetMood: setPetMood,
      startShift: startShift,
      clockOut: clockOut,
      selectTicket: selectTicket,
      selectProduct: selectProduct,
      selectColor: selectColor,
      setQty: setQty,
      addToCart: addToCart,
      removeFromCart: removeFromCart,
      clearCart: clearCart,
      placeOrder: placeOrder,
      returnsChoice: returnsChoice,
      tick: tick
    };
  }

  return { createGame: createGame, COLOR_HEX: COLOR_HEX, SHIFT_SECONDS: SHIFT_SECONDS };
});
