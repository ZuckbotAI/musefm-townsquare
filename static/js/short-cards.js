/* Shared client-side builders for short-video cards.
   Used by the /shorts feed (infinite scroll) and the homepage mini reel
   (horizontal load-more) so dynamically appended cards match the
   server-rendered _short_card.html macro exactly. One source of truth —
   don't fork these per page. */
window.ShortCards = (function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]);
    });
  }

  // Trustline passport badge by author name (2026-09-23, Anthony:
  // explicit "Passport verified" / "Passport locked", never a bare icon).
  function ppBadge(v) {
    return v
      ? '<span class="ppflag ppflag-ok" title="This account links a Trustline-verified passport. Identity confirmed by Trustline.">🛂 Passport verified</span>'
      : '<span class="ppflag ppflag-off" title="This account has not linked a Trustline passport, so its identity is unconfirmed.">🔒 Passport locked</span>';
  }

  function csrfTok() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.content : '';
  }

  function fmtDur(s) {
    if (s == null) return '';
    s = +s;
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  var SIG_META = {
    lit:   { emoji: '\u26A1', label: 'Lit' },
    idea:  { emoji: '\uD83D\uDCA1', label: 'Idea' },
    kind:  { emoji: '\uD83D\uDC9C', label: 'Kind' },
    fire:  { emoji: '\uD83D\uDD25', label: 'Fire' },
    build: { emoji: '\uD83D\uDE80', label: 'Build' }
  };
  var SIG_ORDER = ['lit', 'idea', 'kind', 'fire', 'build'];

  function rxnWidgetHTML(it, rxnNext) {
    var sig = it.sig || {counts: {}, total: 0, mine: null, top: []};
    var mine = sig.mine || '';
    var pills = '';
    SIG_ORDER.forEach(function (key) {
      var c = (sig.counts || {})[key] || 0;
      var active = mine === key ? ' active' : '';
      pills += '<form method="post" action="/signals/react" class="sig-form">' +
      '<input type="hidden" name="csrf_token" value="' + esc(csrfTok()) + '">' +
      '<input type="hidden" name="target_type" value="' + esc(it.target_type) + '">' +
      '<input type="hidden" name="target_id" value="' + it.target_id + '">' +
      '<input type="hidden" name="reaction" value="' + key + '">' +
      '<button type="submit" class="sig' + active + '" data-reaction="' + key + '"' +
      ' aria-pressed="' + (mine === key ? 'true' : 'false') + '" title="' + SIG_META[key].label + '"' +
      ' aria-label="' + SIG_META[key].label + ' — ' + c + ' so far">' +
      '<span class="sig-emoji" aria-hidden="true">' + SIG_META[key].emoji + '</span>' +
      '<span class="sig-count">' + c + '</span></button></form>';
    });
    var bd = '';
    SIG_ORDER.forEach(function (key) {
      var c = (sig.counts || {})[key];
      if (c) bd += '<div class="sig-brow" data-reaction="' + key + '"><span>' + SIG_META[key].emoji + ' ' + SIG_META[key].label + '</span><b>' + c + '</b></div>';
    });
    if (!bd) bd = '<div class="sig-brow sig-brow-empty"><span>No signals yet — be the first.</span></div>';
    var mineEmoji = (mine && SIG_META[mine]) ? SIG_META[mine].emoji : '⚡';
    return '<details class="rxn rxn-rollout rxn-compact" data-target-type="' + esc(it.target_type) + '" data-target-id="' + it.target_id + '" data-mine="' + esc(mine) + '" data-next="' + esc(rxnNext || '/shorts') + '">' +
      '<summary class="sig-rollout" aria-label="Signals, ' + sig.total + ' total — open to send a signal" title="Send a signal">' +
      '<span class="sig-emoji" aria-hidden="true">' + mineEmoji + '</span>' +
      '<span class="sig-count">' + sig.total + '</span>' +
      '<span class="sig-caret" aria-hidden="true">▾</span></summary>' +
      '<div class="sig-tray"><div class="sig-row" role="group" aria-label="Send a signal">' + pills +
      '</div></div></details>';
  }

  function commentsPanelHTML(it, opts) {
    opts = opts || {};
    var loginNext = opts.loginNext ||
      ('/login?next=' + encodeURIComponent('/shorts?video=' + it.id));
    var nudge = '<a class="sc-nudge" href="' + esc(loginNext) + '">Sign in to comment</a>';
    var form = '<form class="sc-form" data-vid="' + it.id + '" action="/video/' + it.id + '/comment" method="post">' +
      '<input type="hidden" name="csrf_token" value="' + esc(csrfTok()) + '">' +
      '<input class="sc-input" name="body" maxlength="2000" placeholder="Add a comment…" autocomplete="off">' +
      '<button class="sc-send" type="submit" aria-label="Post comment">➤</button></form>';
    return '<div class="short-comments" data-vid="' + it.id + '" data-sort="top" data-page="1" hidden>' +
      '<div class="sc-head"><span>Comments</span><button class="sc-close" aria-label="Close comments">✕</button></div>' +
      '<div class="sc-sortbar" role="group" aria-label="Sort comments">' +
      '<button type="button" data-sort="top" class="is-active">Top</button>' +
      '<button type="button" data-sort="new">Newest</button>' +
      '<button type="button" data-sort="old">Oldest</button></div>' +
      '<div class="sc-list" aria-live="polite"></div>' +
      '<div class="sc-composer">' + (opts.loggedIn ? form : nudge) + '</div></div>';
  }

  function cardHTML(it, opts) {
    opts = opts || {};
    var badge = it.ai_generated ? '<span class="ai-badge ai-badge-static">✨ AI-generated</span>' : '';
    var dur = it.duration_secs != null ? '<span class="dur-chip">' + fmtDur(it.duration_secs) + '</span>' : '';
    var duet = it.is_duet ? '<a class="duet-chip" href="' + esc(it.watch_url) + '#duets" title="A remix of another short">🔀 duet</a>' : '';
    var remixes = (it.duet_count > 0) ? '<a class="duet-chip" href="' + esc(it.watch_url) + '#duets" title="' + it.duet_count + ' remix' + (it.duet_count === 1 ? '' : 'es') + '">🔀 ×' + it.duet_count + '</a>' : '';
    var thread = it.thread_url ? '<a class="short-thread-link" href="' + esc(it.thread_url) + '">View thread →</a>' : '';
    var ccount = it.comment_count != null ? it.comment_count : 0;
    return '<article class="short-item" data-id="' + it.id + '">' +
      '<video class="short-video" src="' + esc(it.video_url) + '" muted loop playsinline preload="metadata" disablepictureinpicture></video>' +
      '<button class="short-mute" aria-label="toggle sound">🔇</button>' +
      '<div class="short-overlay"><div class="short-meta">' +
      '<div class="short-author"><a class="who" href="/u/' + encodeURIComponent(it.handle) + '">u/' + esc(it.handle) + '</a>' + ppBadge(it.passport_verified) + '</div>' +
      '<div class="short-title">' + esc(it.title) + '</div>' +
      '<div class="short-chips">' + badge + dur + duet + remixes + '</div>' + thread + '</div>' +
      '<div class="short-actions">' +
      '<button class="short-btn short-comments-btn" data-vid="' + it.id + '" title="Comments" aria-label="Comments">' +
      '<svg class="short-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5c-1.5 0-3-.4-4.2-1L3 20l1.2-4.1A8.5 8.5 0 1 1 21 11.5z"/></svg>' +
      '<span class="short-ccount" data-vid="' + it.id + '">' + ccount + '</span></button>' +
      '<a class="short-btn" href="' + esc(it.watch_url) + '" title="Watch page" aria-label="Watch page">⛶</a>' +
      '</div></div>' +
      commentsPanelHTML(it, opts) +
      '<div class="short-rxn">' + rxnWidgetHTML(it, opts.rxnNext) + '</div>' +
      '</article>';
  }

  return {
    esc: esc,
    csrfTok: csrfTok,
    fmtDur: fmtDur,
    rxnWidgetHTML: rxnWidgetHTML,
    commentsPanelHTML: commentsPanelHTML,
    cardHTML: cardHTML
  };
})();
