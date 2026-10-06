/* Amazin' Rush - UI layer. Binds the engine (engine.js) to the DOM.
 * All markup lives under .amazin-rush. No global styles touched here.
 */
(function () {
  'use strict';

  var Engine = (typeof AmazinEngine !== 'undefined') ? AmazinEngine
    : (typeof require === 'function' ? require('./engine.js') : null);

  /* =====================================================================
   * PET INJECTION POINT (plan section 4)
   * renderPet() returns an SVG string for the sidekick corner. The game
   * calls it once at shift start and inserts the result into the pet corner.
   * Later, the real canonical pet SVG plugs in here with no other changes.
   * setPetMood(mood) drives pet states from game events:
   *   idle (gentle bob) | happy (bounce, perfect order)
   *   sleepy (20s no input) | sad (wrong pick / expired ticket, transient)
   * ===================================================================== */
  function renderPet() {
    return '' +
      '<svg class="ar-pet-svg" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-label="pet sidekick">' +
      '<defs>' +
      '<radialGradient id="arp-body" cx="50%" cy="38%" r="65%">' +
      '<stop offset="0%" stop-color="#fbbf24" stop-opacity="0.85"/>' +
      '<stop offset="45%" stop-color="#7dd3fc" stop-opacity="0.95"/>' +
      '<stop offset="100%" stop-color="#0284c7" stop-opacity="1"/>' +
      '</radialGradient>' +
      '</defs>' +
      '<ellipse cx="32" cy="56" rx="16" ry="4" fill="#0f172a" opacity="0.15"/>' +
      '<path d="M32 6 C32 6 14 30 14 42 a18 18 0 0 0 36 0 C50 30 32 6 32 6 Z" fill="url(#arp-body)"/>' +
      '<ellipse cx="25" cy="30" rx="5" ry="8" fill="#ffffff" opacity="0.35" transform="rotate(-18 25 30)"/>' +
      '<g class="ar-eyes">' +
      '<circle cx="25" cy="42" r="4.4" fill="#0f172a"/>' +
      '<circle cx="39" cy="42" r="4.4" fill="#0f172a"/>' +
      '<circle cx="26.4" cy="40.6" r="1.5" fill="#ffffff"/>' +
      '<circle cx="40.4" cy="40.6" r="1.5" fill="#ffffff"/>' +
      '</g>' +
      '<path class="ar-smile" d="M26 50 Q32 54.5 38 50" stroke="#0f172a" stroke-width="2.2" fill="none" stroke-linecap="round"/>' +
      '<g class="ar-zzz-host"></g>' +
      '</svg>' +
      '<span class="ar-zzz">z</span>';
  }

  var petEl = null;
  var petMoodTimer = null;

  function setPetMood(mood) {
    if (!petEl) return;
    petEl.classList.remove('ar-idle', 'ar-happy', 'ar-sad', 'ar-sleepy');
    if (petMoodTimer) { clearTimeout(petMoodTimer); petMoodTimer = null; }
    if (mood === 'happy' || mood === 'sad') {
      // transient: play the animation, then settle back to idle
      petEl.classList.add(mood === 'happy' ? 'ar-happy' : 'ar-sad');
      petMoodTimer = setTimeout(function () {
        if (!petEl) return;
        petEl.classList.remove('ar-happy', 'ar-sad');
        petEl.classList.add('ar-idle');
      }, 1500);
    } else {
      petEl.classList.add(mood === 'sleepy' ? 'ar-sleepy' : 'ar-idle');
    }
  }
  /* ============================ end pet injection point ============================ */

  /* Placeholder tile for any SKU whose image fails to load (plan section 3):
   * glossy rounded square with the product initial. Never breaks the grid. */
  function placeholderURI(name, hex) {
    var initial = (name || '?').trim().charAt(0).toUpperCase();
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">' +
      '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="#ffffff" stop-opacity="0.55"/>' +
      '<stop offset="0.45" stop-color="#ffffff" stop-opacity="0.08"/>' +
      '<stop offset="1" stop-color="#000000" stop-opacity="0.18"/>' +
      '</linearGradient></defs>' +
      '<rect x="4" y="4" width="120" height="120" rx="26" fill="' + hex + '"/>' +
      '<rect x="4" y="4" width="120" height="120" rx="26" fill="url(#g)"/>' +
      '<text x="64" y="86" font-family="sans-serif" font-size="64" font-weight="900" ' +
      'text-anchor="middle" fill="#ffffff" opacity="0.92">' + initial + '</text>' +
      '</svg>';
    return 'data:image/svg+xml,' + encodeURIComponent(svg);
  }

  function el(tag, cls, html) {
    var d = document.createElement(tag);
    if (cls) d.className = cls;
    if (html != null) d.innerHTML = html;
    return d;
  }

  function fmtTime(s) {
    s = Math.max(0, Math.ceil(s));
    var m = Math.floor(s / 60), r = s % 60;
    return m + ':' + (r < 10 ? '0' : '') + r;
  }

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  /* Catalog loading: bundled at build time; also attempts fetch('items.json')
   * for future catalog swaps, adopting it only if its images actually load. */
  function loadCatalog() {
    var bundled = (typeof window.AMAZIN_CATALOG !== 'undefined') ? window.AMAZIN_CATALOG : [];
    function probe(items) {
      // check the first few images really resolve before adopting a swap
      var checks = items.slice(0, 3).map(function (it) {
        return new Promise(function (res) {
          var img = new Image();
          var done = false;
          var to = setTimeout(function () { if (!done) { done = true; res(false); } }, 2500);
          img.onload = function () { if (!done) { done = true; clearTimeout(to); res(true); } };
          img.onerror = function () { if (!done) { done = true; clearTimeout(to); res(false); } };
          img.src = it.image;
        });
      });
      return Promise.all(checks).then(function (rs) {
        return rs.every(Boolean) ? items : bundled;
      });
    }
    // file:// can't fetch (and logs console noise): skip the swap attempt there,
    // the bundled catalog is the build-time bundle per the plan.
    if (typeof fetch === 'function' && window.location.protocol !== 'file:') {
      return fetch('/static/games/amazin/items.json').then(function (r) {
        if (!r.ok) throw new Error('no items.json');
        return r.json();
      }).then(function (j) {
        if (!Array.isArray(j) || !j.length || !j[0].sku) throw new Error('bad catalog');
        return probe(j).catch(function () { return bundled; });
      }).catch(function () { return bundled; });
    }
    return Promise.resolve(bundled);
  }

  function boot(catalog) {
    var game = Engine.createGame(catalog, {});
    var root = document.querySelector('.amazin-rush');
    if (!root) return;
    if (typeof window !== 'undefined') window.__arGame = game; // test hook

    var searchText = '';
    var activeCat = 'All';
    var ticketEls = {};   // ticketId -> {card, fill, frozenTag}
    var toastTimer = null;

    /* ---------- static DOM ---------- */
    var titleScr = el('div', 'ar-screen ar-title ar-on');
    titleScr.innerHTML =
      '<div class="ar-title-kicker">STACK HOUSE RUSH</div>' +
      '<h1 class="ar-title-h1">Shift 1 of 1</h1>' +
      '<p class="ar-title-sub">The floor is chaos, the tickets keep coming, and somebody ordered navy when they meant rust.</p>' +
      '<ul class="ar-howto ar-hidden" id="arHowto">' +
      '<li>Tickets on the rail. Tap one, search the catalog, bag the match. Color and count.</li>' +
      '<li>Decoy colorways sit next to each other. Navy is not rust. Over-ear is not earbuds.</li>' +
      '<li>If they freeze and say wait, place the original for a fat tip. Miss it and they swap. Returns still have a policy.</li>' +
      '</ul>' +
      '<button class="ar-btn" id="arOpen">Open the floor</button>' +
      '<div class="ar-title-row">' +
      '<button class="ar-btn ar-ghost" id="arHow">How it works</button>' +
      '<button class="ar-btn ar-ghost" id="arQuit">Clock out</button>' +
      '</div>';

    var gameScr = el('div', 'ar-screen ar-game');
    gameScr.innerHTML =
      '<div class="ar-topbar">' +
      '<div class="ar-timer" id="arTimer">3:00</div>' +
      '<div class="ar-score">score <span class="ar-score-num" id="arScore">0</span></div>' +
      '<div class="ar-mult" id="arMult">1x</div>' +
      '<button class="ar-mute" id="arMute" aria-label="Toggle sound">🔊</button>' +
      '<button class="ar-clockout" id="arClockout">clock out</button>' +
      '</div>' +
      '<div class="ar-rail" id="arRail"></div>' +
      '<div class="ar-searchrow"><input class="ar-search" id="arSearch" type="search" placeholder="Search amazin" autocomplete="off"></div>' +
      '<div class="ar-chips" id="arChips"></div>' +
      '<div class="ar-grid" id="arGrid"></div>' +
      '<div class="ar-tray" id="arTray"></div>' +
      '<div class="ar-bottombar">' +
      '<div class="ar-cart" id="arCart"></div>' +
      '<button class="ar-place" id="arPlace">Place order</button>' +
      '</div>' +
      '<div class="ar-pet ar-idle" id="arPet"></div>' +
      '<div class="ar-modal-wrap" id="arModalWrap"><div class="ar-modal" id="arModal"></div></div>' +
      '<div class="ar-toast" id="arToast"></div>';

    var overScr = el('div', 'ar-screen ar-over');
    overScr.innerHTML =
      '<div class="ar-over-kicker">SHIFT OVER</div>' +
      '<p class="ar-over-score" id="arFinalScore">0</p>' +
      '<p class="ar-over-label">final score</p>' +
      '<div class="ar-statgrid" id="arStats"></div>' +
      '<button class="ar-btn" id="arAgain">Clock in again</button>' +
      '<button class="ar-btn ar-ghost" id="arOverQuit">Clock out</button>';

    root.appendChild(titleScr);
    root.appendChild(gameScr);
    root.appendChild(overScr);

    var timerEl = gameScr.querySelector('#arTimer');
    var scoreEl = gameScr.querySelector('#arScore');
    var multEl = gameScr.querySelector('#arMult');
    var railEl = gameScr.querySelector('#arRail');
    var searchEl = gameScr.querySelector('#arSearch');
    var chipsEl = gameScr.querySelector('#arChips');
    var gridEl = gameScr.querySelector('#arGrid');
    var trayEl = gameScr.querySelector('#arTray');
    var cartEl = gameScr.querySelector('#arCart');
    var modalWrap = gameScr.querySelector('#arModalWrap');
    var modalBox = gameScr.querySelector('#arModal');
    var toastEl = gameScr.querySelector('#arToast');
    petEl = gameScr.querySelector('#arPet');

    function show(scr) {
      [titleScr, gameScr, overScr].forEach(function (s) { s.classList.remove('ar-on'); });
      scr.classList.add('ar-on');
    }

    function toast(msg) {
      toastEl.textContent = msg;
      toastEl.classList.add('ar-on');
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(function () { toastEl.classList.remove('ar-on'); }, 1800);
    }

    function imgFor(rec, cls) {
      var img = el('img', cls);
      img.alt = rec.name;
      img.loading = 'lazy';
      var hex = Engine.COLOR_HEX[rec.color] || '#94a3b8';
      img.onerror = function () {
        img.onerror = null;
        img.src = placeholderURI(rec.name, hex);
      };
      img.src = rec.image;
      return img;
    }

    /* ---------- tickets ---------- */
    function renderTickets() {
      // reconcile: add new, drop gone
      var seen = {};
      game.state.tickets.forEach(function (t) {
        seen[t.id] = true;
        if (ticketEls[t.id]) {
          var rec = ticketEls[t.id];
          rec.card.classList.toggle('ar-active', game.state.activeTicketId === t.id);
          var fz = rec.card.querySelector('.ar-frozen-tag');
          if (fz) fz.style.display = t.frozen ? 'inline-block' : 'none';
          return;
        }
        var card = el('div', 'ar-ticket' + (game.state.activeTicketId === t.id ? ' ar-active' : ''));
        var linesHtml = t.lines.map(function (l) {
          return '<li class="ar-line">' + esc(game.lineText(l)) + '</li>';
        }).join('');
        card.innerHTML =
          '<div class="ar-ticket-head">' +
          '<div class="ar-avatar" style="background:' + Engine.COLOR_HEX[t.avatarColor] + '">' +
          esc(t.name.charAt(0)) + '</div>' +
          '<div><div class="ar-ticket-name">' + esc(t.name) + '</div>' +
          '<div class="ar-ticket-quirk">' + esc(t.quirk) + '</div></div>' +
          '<div class="ar-ticket-active-tag">FILLING</div>' +
          '</div>' +
          (t.frozen ? '<span class="ar-frozen-tag">wait...</span>' : '<span class="ar-frozen-tag" style="display:none">wait...</span>') +
          '<ul class="ar-lines">' + linesHtml + '</ul>' +
          '<div class="ar-patience"><div class="ar-patience-fill"></div></div>';
        card.addEventListener('click', function () {
          game.selectTicket(t.id);
          renderTickets();
        });
        railEl.appendChild(card);
        ticketEls[t.id] = {
          card: card,
          fill: card.querySelector('.ar-patience-fill'),
          bar: card.querySelector('.ar-patience'),
          frozenTag: card.querySelector('.ar-frozen-tag')
        };
      });
      Object.keys(ticketEls).forEach(function (id) {
        if (!seen[id]) {
          var r = ticketEls[id];
          if (r.card.parentNode) r.card.parentNode.removeChild(r.card);
          delete ticketEls[id];
        }
      });
      if (game.state.tickets.length === 0 && !railEl.querySelector('.ar-rail-empty')) {
        var e = el('div', 'ar-rail-empty', game.state.timeLeft > 0 ? 'Waiting on customers...' : 'No open tickets.');
        railEl.appendChild(e);
      } else if (game.state.tickets.length > 0) {
        var old = railEl.querySelector('.ar-rail-empty');
        if (old) old.parentNode.removeChild(old);
      }
    }

    function updateBars() {
      game.state.tickets.forEach(function (t) {
        var r = ticketEls[t.id];
        if (!r) return;
        var frac = Math.max(0, t.patience / t.patienceMax);
        r.fill.style.transform = 'scaleX(' + frac.toFixed(3) + ')';
        r.bar.classList.toggle('ar-crit', frac < 0.25);
        if (r.frozenTag) r.frozenTag.style.display = t.frozen ? 'inline-block' : 'none';
      });
      timerEl.textContent = fmtTime(game.state.timeLeft);
      timerEl.classList.toggle('ar-low', game.state.timeLeft <= 30 && game.state.timeLeft > 0);
      scoreEl.textContent = game.state.score;
      multEl.textContent = game.state.multiplier + 'x';
      multEl.classList.toggle('ar-hot', game.state.multiplier >= 4);
    }

    /* ---------- product grid ---------- */
    function filteredProducts() {
      var q = searchText.trim().toLowerCase();
      return game.products.filter(function (p) {
        if (activeCat !== 'All' && p.category !== activeCat) return false;
        if (q && p.name.toLowerCase().indexOf(q) === -1) return false;
        return true;
      });
    }

    function renderGrid() {
      gridEl.innerHTML = '';
      var list = filteredProducts();
      if (!list.length) {
        gridEl.appendChild(el('div', 'ar-grid-empty', 'Nothing matches. Try another search.'));
        return;
      }
      list.forEach(function (p) {
        var card = el('div', 'ar-card');
        var img = imgFor(p.colorways[0], 'ar-card-img');
        card.appendChild(img);
        card.appendChild(el('div', 'ar-card-name', esc(p.name)));
        var meta = el('div', 'ar-card-meta');
        meta.appendChild(el('div', 'ar-card-price', '$' + p.colorways[0].price));
        if (p.colorways.some(function (c) { return c.prime; })) {
          meta.appendChild(el('div', 'ar-prime', 'PRIME'));
        }
        card.appendChild(meta);
        var dots = el('div', 'ar-dots');
        p.colorways.forEach(function (cw) {
          var d = el('button', 'ar-dot' + (game.state.selectedSku === cw.sku ? ' ar-on' : ''));
          d.style.background = Engine.COLOR_HEX[cw.color];
          d.title = cw.color;
          d.setAttribute('aria-label', cw.color);
          d.addEventListener('click', function (ev) {
            ev.stopPropagation();
            game.selectProduct(cw.sku);
            renderGrid();
            renderTray();
          });
          dots.appendChild(d);
        });
        card.appendChild(dots);
        var sel = p.colorways.some(function (cw) { return cw.sku === game.state.selectedSku; });
        if (sel) card.classList.add('ar-sel');
        card.addEventListener('click', function () {
          game.selectProduct(p.colorways[0].sku);
          renderGrid();
          renderTray();
        });
        gridEl.appendChild(card);
      });
    }

    function renderChips() {
      chipsEl.innerHTML = '';
      ['All'].concat(game.categories).forEach(function (c) {
        var b = el('button', 'ar-chip' + (activeCat === c ? ' ar-on' : ''), esc(c));
        b.addEventListener('click', function () {
          activeCat = c;
          game.touch();
          renderChips();
          renderGrid();
        });
        chipsEl.appendChild(b);
      });
    }

    /* ---------- selection tray ---------- */
    function renderTray() {
      trayEl.innerHTML = '';
      var rec = game.bySku[game.state.selectedSku];
      if (!rec) { trayEl.classList.remove('ar-on'); return; }
      trayEl.classList.add('ar-on');
      var prod = game.products.filter(function (p) { return p.name === rec.name; })[0];
      trayEl.appendChild(imgFor(rec, 'ar-tray-img'));
      var info = el('div', 'ar-tray-info');
      info.appendChild(el('div', 'ar-tray-name', esc(rec.name)));
      info.appendChild(el('div', 'ar-tray-price', '$' + rec.price + (rec.prime ? ' PRIME' : '')));
      var dots = el('div', 'ar-dots');
      prod.colorways.forEach(function (cw) {
        var d = el('button', 'ar-dot' + (cw.sku === game.state.selectedSku ? ' ar-on' : ''));
        d.style.background = Engine.COLOR_HEX[cw.color];
        d.title = cw.color;
        d.setAttribute('aria-label', cw.color);
        d.addEventListener('click', function () {
          game.selectColor(cw.color);
          renderTray();
          renderGrid();
        });
        dots.appendChild(d);
      });
      info.appendChild(dots);
      trayEl.appendChild(info);

      var stepper = el('div', 'ar-stepper');
      var minus = el('button', 'ar-step-btn', '−');
      var qtyEl = el('div', 'ar-qty', String(game.state.selectedQty));
      var plus = el('button', 'ar-step-btn', '+');
      minus.addEventListener('click', function () {
        qtyEl.textContent = String(game.setQty(game.state.selectedQty - 1));
      });
      plus.addEventListener('click', function () {
        qtyEl.textContent = String(game.setQty(game.state.selectedQty + 1));
      });
      stepper.appendChild(minus);
      stepper.appendChild(qtyEl);
      stepper.appendChild(plus);
      trayEl.appendChild(stepper);

      var bag = el('button', 'ar-bag', 'Bag it');
      bag.addEventListener('click', function () {
        var r = game.addToCart();
        if (r.ok) { renderCart(); toast('Bagged ' + rec.color + ' ' + rec.name + ' x' + game.state.selectedQty); }
      });
      trayEl.appendChild(bag);
    }

    /* ---------- cart ---------- */
    function renderCart() {
      cartEl.innerHTML = '';
      if (!game.state.cart.length) {
        cartEl.appendChild(el('div', 'ar-cart-empty', 'Cart is empty. Bag items from the grid.'));
        return;
      }
      game.state.cart.forEach(function (cl) {
        var rec = game.bySku[cl.sku];
        if (!rec) return;
        var line = el('div', 'ar-cartline');
        line.title = rec.color + ' ' + rec.name;
        line.appendChild(imgFor(rec, 'ar-cart-img'));
        line.appendChild(el('div', 'ar-cart-qty', 'x' + cl.qty));
        var x = el('div', 'ar-cart-x', '×');
        line.appendChild(x);
        line.addEventListener('click', function () {
          game.removeFromCart(cl.sku);
          renderCart();
        });
        cartEl.appendChild(line);
      });
    }

    /* ---------- returns modal ---------- */
    function renderModal() {
      var m = game.state.modal;
      if (!m) { modalWrap.classList.remove('ar-on'); return; }
      var t = null;
      game.state.tickets.forEach(function (tk) { if (tk.id === m.ticketId) t = tk; });
      var probs = m.problems.map(function (p) {
        return '<li class="ar-mp">' + esc(game.describeProblem(p)) + '</li>';
      }).join('');
      modalBox.innerHTML =
        '<h2 class="ar-modal-h">Return at the counter</h2>' +
        '<p class="ar-modal-sub">' + (t ? esc(t.name) + ' is not happy with this bag.' : 'The customer is not happy.') + '</p>' +
        '<ul class="ar-modal-problems">' + probs + '</ul>' +
        '<div class="ar-modal-btns">' +
        '<button class="ar-mbtn ar-refund" data-c="refund">Refund (-50 pts, patience back up)</button>' +
        '<button class="ar-mbtn ar-exchange" data-c="exchange">Exchange (one more try)</button>' +
        '<button class="ar-mbtn ar-deny" data-c="deny">Deny (they storm off)</button>' +
        '</div>';
      modalBox.querySelectorAll('.ar-mbtn').forEach(function (b) {
        b.addEventListener('click', function () {
          game.returnsChoice(b.getAttribute('data-c'));
          renderModal();
          renderTickets();
          renderCart();
          updateBars();
        });
      });
      modalWrap.classList.add('ar-on');
    }

    /* ---------- shift over ---------- */
    function renderOver() {
      var s = game.state.stats;
      if (!s) return;
      overScr.querySelector('#arFinalScore').textContent = s.score;
      var st = overScr.querySelector('#arStats');
      st.innerHTML = '';
      [
        [s.ordersCompleted, 'orders done'],
        [s.accuracy + '%', 'accuracy'],
        [s.bestStreak, 'best streak'],
        [s.perfectOrders, 'perfect bags']
      ].forEach(function (pair) {
        var d = el('div', 'ar-stat');
        d.innerHTML = '<div class="ar-stat-v">' + pair[0] + '</div><div class="ar-stat-k">' + pair[1] + '</div>';
        st.appendChild(d);
      });
    }

    function fullRender() {
      renderTickets();
      renderGrid();
      renderTray();
      renderCart();
      renderModal();
      updateBars();
    }

    /* ---------- engine events ---------- */
    var sfx = (typeof window !== 'undefined' && window.AmazinSfx) ? window.AmazinSfx : { play: function () {}, toggle: function () { return false; }, muted: false };
    game.on(function (type, data) {
      if (type === 'petMood') { setPetMood(data.mood); return; }
      if (type === 'shiftStart') { sfx.play('start'); }
      else if (type === 'shiftOver') { sfx.play('end'); }
      else if (type === 'ticketSpawn') { sfx.play('ding'); }
      else if (type === 'ticketExpired') { sfx.play('sad'); }
      else if (type === 'ticketSelect') { sfx.play('ticket'); }
      else if (type === 'productSelect') { sfx.play('select'); }
      else if (type === 'cart') { sfx.play('pop'); }
      else if (type === 'orderComplete') { sfx.play(data && data.perfect ? 'fanfare' : 'chaChing'); }
      else if (type === 'returnsOpen') { sfx.play('alert'); }
      else if (type === 'multiplier' && data && data.multiplier > 1) { sfx.play('streak'); }
      if (type === 'shiftStart') {
        ticketEls = {};
        railEl.innerHTML = '';
        searchEl.value = '';
        searchText = '';
        activeCat = 'All';
        petEl.innerHTML = renderPet();
        setPetMood('idle');
        renderChips();
        fullRender();
        show(gameScr);
        return;
      }
      if (type === 'shiftOver') {
        renderOver();
        show(overScr);
        return;
      }
      if (type === 'ticketSpawn' || type === 'ticketExpired' || type === 'ticketSwap' ||
          type === 'orderComplete' || type === 'returnsOpen' || type === 'returnsResolved') {
        renderTickets();
        renderCart();
        renderModal();
        updateBars();
      }
      if (type === 'orderComplete') {
        var bits = ['+' + data.earned + ' pts'];
        if (data.perfect) bits.push('perfect');
        if (data.frozenTip) bits.push('patient tip');
        toast(bits.join(' · '));
      }
      if (type === 'ticketExpired') toast('A customer stormed out.');
      if (type === 'ticketSwap') toast('They changed their mind!');
    });

    /* ---------- wiring ---------- */
    titleScr.querySelector('#arOpen').addEventListener('click', function () { game.startShift(); });
    titleScr.querySelector('#arHow').addEventListener('click', function () {
      titleScr.querySelector('#arHowto').classList.toggle('ar-hidden');
    });
    titleScr.querySelector('#arQuit').addEventListener('click', function () {
      toast('No shift running. The floor misses you.');
    });
    var muteBtn = gameScr.querySelector('#arMute');
    function syncMute() { muteBtn.textContent = sfx.muted ? '🔇' : '🔊'; muteBtn.classList.toggle('ar-off', !!sfx.muted); }
    syncMute();
    muteBtn.addEventListener('click', function () { sfx.toggle(); syncMute(); sfx.play('click'); });
    gameScr.querySelector('#arClockout').addEventListener('click', function () {
      game.clockOut();
      show(titleScr);
    });
    overScr.querySelector('#arAgain').addEventListener('click', function () { game.startShift(); });
    overScr.querySelector('#arOverQuit').addEventListener('click', function () {
      game.clockOut();
      show(titleScr);
    });
    searchEl.addEventListener('input', function () {
      searchText = searchEl.value;
      game.touch();
      renderGrid();
    });
    gameScr.querySelector('#arPlace').addEventListener('click', function () {
      var r = game.placeOrder();
      if (r.ok) { fullRender(); return; }
      if (r.reason === 'no-ticket') toast('Tap a ticket on the rail first.');
      else if (r.reason === 'empty-cart') toast('Bag something from the grid first.');
      else if (r.reason === 'returns') { renderModal(); renderTickets(); updateBars(); }
    });

    /* ---------- main loop ---------- */
    var last = null;
    function frame(ts) {
      if (last == null) last = ts;
      var dt = Math.min(0.25, (ts - last) / 1000);
      last = ts;
      if (game.state.screen === 'playing') {
        game.tick(dt);
        updateBars();
      }
      requestAnimationFrame(frame);
    }

    renderChips();
    renderGrid();
    petEl.innerHTML = renderPet();
    requestAnimationFrame(frame);
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { loadCatalog().then(boot); });
    } else {
      loadCatalog().then(boot);
    }
  }

  // expose the pet hook for tests / future canonical-pet swap
  if (typeof window !== 'undefined') {
    window.AmazinPet = { renderPet: renderPet, setPetMood: setPetMood };
  }
})();
