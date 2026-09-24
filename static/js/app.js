/* MuseFM — shared UI helpers */
function toggleTheme() {
  var el = document.documentElement;
  var next = el.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  el.setAttribute('data-theme', next);
  try { localStorage.setItem('ts-theme', next); } catch (e) {}
}

// ---- left sidebar drawer (mobile) ----
function toggleSidebar(force) {
  var open = typeof force === 'boolean' ? force : !document.body.classList.contains('sidebar-open');
  document.body.classList.toggle('sidebar-open', open);
  var b = document.getElementById('sb-toggle');
  if (b) b.setAttribute('aria-expanded', open ? 'true' : 'false');
}
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' && document.body.classList.contains('sidebar-open')) toggleSidebar(false);
});
// tapping a sidebar link closes the drawer
document.addEventListener('click', function (e) {
  var a = e.target && e.target.closest ? e.target.closest('.sidebar a') : null;
  if (a && document.body.classList.contains('sidebar-open')) toggleSidebar(false);
});

function toast(msg) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(function(){ t.classList.remove('show'); }, 2200);
}

// show remembered handle in nav — only when the server didn't already
// render one (the server prefers the logged-in session handle; a stale
// cookie must never clobber it). (2026-09-23: "u/anon" header bug)
(function () {
  var m = document.cookie.match(/(?:^|;)\s*ts_handle=([^;]+)/);
  var chip = document.getElementById('handle-chip');
  if (m && chip && !chip.textContent.trim()) chip.textContent = 'u/' + decodeURIComponent(m[1]);
})();

// remember handle from any form
document.addEventListener('submit', function (e) {
  var h = e.target.querySelector && e.target.querySelector('input[name="handle"]');
  if (h && h.value) {
    try { localStorage.setItem('ts-handle', h.value); } catch (e2) {}
  }
});
// prefill handle fields from memory
document.addEventListener('DOMContentLoaded', function () {
  var saved = null;
  try { saved = localStorage.getItem('ts-handle'); } catch (e) {}
  if (saved) document.querySelectorAll('input[name="handle"]').forEach(function (i) {
    if (!i.value) i.value = saved;
  });
});

// ---- share sheet ----
var shareCtx = null;
function openShare(slug, title) {
  shareCtx = { slug: slug, title: title };
  var t = 0;
  if (window.TSPlayer && TSPlayer.current && TSPlayer.current.slug === slug) {
    t = Math.floor(TSPlayer.current.el.currentTime || 0);
  }
  var url = location.origin + '/episodes#' + slug + (t > 3 ? '?t=' + t : '');
  var opts = document.getElementById('share-opts');
  opts.innerHTML = '';
  var items = [
    ['⧉', 'Copy link' + (t > 3 ? ' (at ' + fmtT(t) + ')' : ''), function () {
      copyText(url); toast('Link copied'); closeShare();
    }],
    ['𝕏', 'Share on X', function () {
      // NOTE: page URL goes inside `text` — X's composer pulls text
      // reliably but was dropping the separate `url` param.
      window.open('https://x.com/intent/tweet?text=' +
        encodeURIComponent('🎙️ ' + title + ' — MuseFM ' + url), '_blank');
      closeShare();
    }]
  ];
  if (navigator.share) items.push(['↗', 'More…', function () {
    navigator.share({ title: title, text: '🎙️ ' + title + ' — MuseFM', url: url });
    closeShare();
  }]);
  items.forEach(function (it) {
    var b = document.createElement('button');
    b.className = 'share-opt';
    b.innerHTML = '<span style="font-size:18px">' + it[0] + '</span><span>' + it[1] + '</span>';
    b.onclick = it[2];
    opts.appendChild(b);
  });
  document.getElementById('share-sheet').classList.add('open');
}
function closeShare() {
  document.getElementById('share-sheet').classList.remove('open');
}
document.getElementById('share-sheet').addEventListener('click', function (e) {
  if (e.target === this) closeShare();
});
function copyText(s) {
  if (navigator.clipboard) navigator.clipboard.writeText(s).catch(function(){ fallbackCopy(s); });
  else fallbackCopy(s);
}
function fallbackCopy(s) {
  var ta = document.createElement('textarea');
  ta.value = s; document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
}
function fmtT(s) {
  s = Math.floor(s); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}
document.addEventListener('click', function (e) {
  var b = e.target.closest('[data-share]');
  if (b) openShare(b.getAttribute('data-share'), b.getAttribute('data-title'));
});

// ---- notification bell popout (opens a panel, not a page) ----
(function () {
  var btn = document.getElementById('notif-bell-btn');
  var pop = document.getElementById('notif-pop');
  if (!btn || !pop) return;
  var list = document.getElementById('notif-pop-list');
  var loaded = false;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]);
    });
  }
  function ago(ts) {
    if (!ts) return '';
    var d = Math.floor(Date.now() / 1000) - ts;
    if (d < 0) d = 0;
    if (d < 60) return 'just now';
    if (d < 3600) return Math.floor(d / 60) + 'm ago';
    if (d < 86400) return Math.floor(d / 3600) + 'h ago';
    if (d < 7 * 86400) return Math.floor(d / 86400) + 'd ago';
    return new Date(ts * 1000).toLocaleDateString();
  }
  function render(items) {
    if (!items.length) {
      list.innerHTML = '<div class="empty-state"><p class="hint">All quiet. Replies and @mentions land here.</p></div>';
      return;
    }
    list.innerHTML = items.map(function (n) {
      var link = n.url ? ' · <a href="' + esc(n.url) + '">' + esc(n.link_label || 'View') + '</a>' : '';
      return '<div class="notif-row">' +
        '<span class="notif-icon" aria-hidden="true">' + esc(n.icon) + '</span>' +
        '<div class="notif-body"><p class="notif-text">' + esc(n.text) + '</p>' +
        '<p class="notif-meta">' + esc(ago(n.created_at)) + link + '</p></div></div>';
    }).join('');
  }
  function open() {
    pop.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    if (loaded) return;
    loaded = true;
    fetch('/api/notifications/mine', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        render((d.ok && d.notifications) || []);
        // fetching marked everything read — drop the badge
        var badge = document.getElementById('notif-badge');
        if (badge && d.ok) badge.remove();
      })
      .catch(function () {
        list.innerHTML = '<div class="empty-state"><p class="hint">Could not load. <a href="/notifications">View all →</a></p></div>';
        loaded = false;
      });
  }
  function close() {
    pop.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
  }
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    pop.hidden ? open() : close();
  });
  document.addEventListener('click', function (e) {
    if (!pop.hidden && !e.target.closest('.notif-wrap')) close();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !pop.hidden) close();
  });
})();
