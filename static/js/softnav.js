/* ==========================================================================
   MuseFM — soft navigation (2026-09-22)
   Same-origin link clicks swap page content in place instead of reloading
   the document. The global Audio element (window.TSPlayer) and the
   mini-player live outside the swapped region, so playback never
   interrupts while browsing.

   Hard navigation (full page load) is used for:
   - external links, downloads, mailto:/tel:
   - modifier / middle / right clicks, target=_blank
   - hash-only jumps on the same page
   - player deep links (/episodes#slug?t=90) — player.js handles those at load
   - forms (login / signup / logout / mutations change session or state)
   - non-HTML responses, fetch failures, or pages whose shell differs
   ========================================================================== */
(function () {
  'use strict';
  if (window.MuseFMSoftNav) return;
  window.MuseFMSoftNav = { version: '2026-09-22' };

  /* Scripts already executed on this document — never re-run them. */
  var seenSrc = {};
  function abs(u) {
    try { return new URL(u, document.baseURI).href; }
    catch (e) { return u; }
  }
  Array.prototype.forEach.call(
    document.querySelectorAll('script[src]'),
    function (s) { seenSrc[abs(s.getAttribute('src'))] = true; }
  );

  /* Head nodes injected by earlier swaps — removed before the next swap. */
  var addedHead = [];
  var navSeq = 0;

  function sameOrigin(u) {
    try { return new URL(u, document.baseURI).origin === location.origin; }
    catch (e) { return false; }
  }

  /* ---- click interception ---- */
  document.addEventListener('click', function (ev) {
    if (ev.defaultPrevented) return;
    var a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
    if (!a) return;
    if (ev.button !== 0) return;
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    if (a.target && a.target !== '_self') return;
    if (a.hasAttribute('download')) return;
    var href = a.getAttribute('href');
    if (!href || href.charAt(0) === '#' ||
        href.indexOf('mailto:') === 0 || href.indexOf('tel:') === 0) return;
    var url = abs(href);
    if (!sameOrigin(url)) return;
    try {
      var cur = new URL(location.href), nxt = new URL(url);
      /* same-page hash jump — let the browser do its thing */
      if (cur.pathname === nxt.pathname && cur.search === nxt.search && nxt.hash) return;
      /* player deep link — player.js handles it on a full load */
      if (nxt.hash && nxt.hash.indexOf('?t=') !== -1) return;
    } catch (e) { /* fall through to soft nav */ }
    ev.preventDefault();
    go(url, true);
  });

  /* ---- back / forward ---- */
  window.addEventListener('popstate', function (ev) {
    if (ev.state && ev.state.musefmSoft) go(location.href, false);
    else location.reload();
  });

  function go(url, push) {
    var my = ++navSeq;
    fetch(url, { credentials: 'same-origin', headers: { 'X-MuseFM-SoftNav': '1' } })
      .then(function (res) {
        if (!res.ok) throw new Error('bad status ' + res.status);
        var ct = res.headers.get('content-type') || '';
        if (ct.indexOf('text/html') === -1) throw new Error('not html');
        return res.text();
      })
      .then(function (html) {
        if (my !== navSeq) return;               /* superseded */
        if (!applySwap(html, url, push)) location.href = url;
      })
      .catch(function () {
        if (my === navSeq) location.href = url;  /* fallback: hard nav */
      });
  }

  function applySwap(html, url, push) {
    var doc;
    try { doc = new DOMParser().parseFromString(html, 'text/html'); }
    catch (e) { return false; }

    var newWrap = doc.querySelector('.wrap');
    var curWrap = document.querySelector('.wrap');
    if (!newWrap || !curWrap) return false;

    /* Shell check: the persistent chrome must exist on the new page. */
    if (!doc.querySelector('#miniplayer') || !doc.querySelector('.sidebar')) return false;

    /* The "New here?" strip lives outside .wrap — sync it or bail out. */
    var newStrip = doc.getElementById('topstrip');
    var curStrip = document.getElementById('topstrip');
    if (!!newStrip !== !!curStrip) {
      if (curStrip) curStrip.remove();
      else return false;   /* would need to add chrome — do a full load */
    }

    /* 1. title + page head assets (stylesheets for pond.css / shop.css …) */
    document.title = doc.title || document.title;
    syncHead(doc);

    /* 2. body classes — page modes like shorts-mode */
    document.body.className = doc.body.className;

    /* 3. swap the content region (player, orb, chrome all survive) */
    curWrap.replaceWith(newWrap);

    /* 4. nav active states (server renders these per request normally) */
    markActiveNav(url);

    /* 5. run the new page's own scripts — globals skipped, already loaded */
    runScripts(doc);

    /* 6. explicit re-hydration hooks */
    try {
      if (window.MuseFMComments && window.MuseFMComments.hydrateTimes)
        window.MuseFMComments.hydrateTimes();
    } catch (e) {}
    try {
      if (window.MuseFMComments && window.MuseFMComments.refreshVoteUI)
        window.MuseFMComments.refreshVoteUI();
    } catch (e) {}
    try {
      if (window.TidepalAnim && window.TidepalAnim.refresh)
        window.TidepalAnim.refresh();
    } catch (e) {}
    prefillHandles();

    /* 7. history, scroll, focus, screen-reader announcement */
    try {
      if (push) history.pushState({ musefmSoft: true }, '', url);
      else history.replaceState({ musefmSoft: true }, '', url);
    } catch (e) {}
    var hash = '';
    try { hash = new URL(url).hash; } catch (e) {}
    if (hash) {
      var t = null;
      try { t = document.getElementById(hash.slice(1).split('?')[0]); } catch (e) {}
      if (t) t.scrollIntoView();
    } else {
      window.scrollTo(0, 0);
    }
    var main = document.querySelector('.wrap');
    if (main) {
      if (!main.hasAttribute('tabindex')) main.setAttribute('tabindex', '-1');
      main.focus({ preventScroll: true });
    }
    announce('Loaded: ' + (doc.title || 'MuseFM'));
    return true;
  }

  /* ---- head sync: add the new page's stylesheets, drop the old page's ---- */
  function syncHead(doc) {
    addedHead.forEach(function (n) {
      if (n.parentNode) n.parentNode.removeChild(n);
    });
    addedHead = [];
    var have = {};
    Array.prototype.forEach.call(
      document.querySelectorAll('link[rel="stylesheet"]'),
      function (l) { have[abs(l.getAttribute('href') || '')] = true; }
    );
    Array.prototype.forEach.call(
      doc.head.querySelectorAll('link[rel="stylesheet"]'),
      function (l) {
        var href = l.getAttribute('href');
        if (!href || have[abs(href)]) return;
        var nl = document.createElement('link');
        nl.rel = 'stylesheet';
        nl.href = href;
        document.head.appendChild(nl);
        addedHead.push(nl);
      }
    );
    Array.prototype.forEach.call(
      doc.head.querySelectorAll('style'),
      function (s) {
        var ns = document.createElement('style');
        ns.textContent = s.textContent;
        document.head.appendChild(ns);
        addedHead.push(ns);
      }
    );
  }

  /* ---- nav active states mirror the server's request.path logic ---- */
  function markActiveNav(url) {
    var path;
    try { path = new URL(url).pathname; } catch (e) { return; }
    function sweep(sel) {
      Array.prototype.forEach.call(
        document.querySelectorAll(sel),
        function (a) {
          var href = a.getAttribute('href');
          if (!href || href.charAt(0) !== '/') return;
          var active = (href === path) || (href !== '/' && path.indexOf(href) === 0);
          a.classList.toggle('active', active);
        }
      );
    }
    sweep('.topbar-nav a');
    sweep('.sidebar a.sb-link');
    sweep('a.notif-bell');
  }

  /* ---- run the new page's scripts ----
     Two passes: external scripts first (async=false keeps their order),
     then inline scripts. Globals from base.html are skipped — they already
     ran. Inline scripts execute against the freshly swapped .wrap. */
  function runScripts(doc) {
    var scripts = doc.body.querySelectorAll('script');
    var inline = [];
    Array.prototype.forEach.call(scripts, function (old) {
      var src = old.getAttribute('src');
      if (src) {
        var key = abs(src);
        if (seenSrc[key]) return;
        seenSrc[key] = true;
        var s = document.createElement('script');
        s.src = key;
        s.async = false;
        document.body.appendChild(s);
      } else {
        var code = old.textContent || '';
        if (code.trim()) inline.push(code);
      }
    });
    inline.forEach(function (code) {
      var ns = document.createElement('script');
      ns.textContent = code;
      document.body.appendChild(ns);
    });
  }

  /* ---- saved-handle prefill (mirrors app.js's DOMContentLoaded pass) ---- */
  function prefillHandles() {
    var saved = null;
    try { saved = localStorage.getItem('ts-handle'); } catch (e) {}
    if (!saved) return;
    Array.prototype.forEach.call(
      document.querySelectorAll('input[name="handle"]'),
      function (inp) { if (!inp.value) inp.value = saved; }
    );
  }

  /* ---- polite page-change announcements ---- */
  var liveEl = null;
  function announce(msg) {
    try {
      if (!liveEl) {
        liveEl = document.createElement('div');
        liveEl.className = 'sr-only';
        liveEl.setAttribute('role', 'status');
        liveEl.setAttribute('aria-live', 'polite');
        document.body.appendChild(liveEl);
      }
      liveEl.textContent = '';
      window.setTimeout(function () { liveEl.textContent = msg; }, 30);
    } catch (e) {}
  }
})();
