/* Shared comment-section behavior: AJAX vote/flag/edit, collapse toggles.
 * Progressive enhancement — every form also works as a plain POST
 * (redirects back) when JS is off or the visitor is signed out.
 */
(function () {
  'use strict';

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    if (m && m.content) return m.content;
    var inp = document.querySelector('input[name="csrf_token"]');
    return inp ? inp.value : '';
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, body: d }; });
    });
  }

  function signedIn() { return !!csrfToken(); }

  /* ---- voting ----
     Owned by static/js/votes.js now: one delegated handler with
     optimistic feedback, per-target lock, and rollback covers every
     form POSTing to /vote (feed posts, comments, Shorts panels).
     refreshVoteUI stays exported for any direct UI syncs. */

  /* ---- flagging ---- */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains('cflag-form')) return;
    if (!signedIn()) return;  // plain POST -> login nudge
    ev.preventDefault();
    var btn = form.querySelector('.cflag-btn');
    if (btn) btn.disabled = true;
    postJSON('/flag', {
      csrf_token: csrfToken(),
      target_type: form.querySelector('[name="target_type"]').value,
      target_id: parseInt(form.querySelector('[name="target_id"]').value, 10),
      reason: 'other'
    }).then(function (res) {
      if (btn) btn.disabled = false;
      if (!res.body.ok) return;
      if (btn) {
        var flagged = res.body.flagged !== false;
        btn.classList.toggle('is-flagged', flagged);
        btn.setAttribute('aria-pressed', flagged ? 'true' : 'false');
        btn.setAttribute('aria-label', flagged ? 'Flagged for review' : 'Flag for moderator review');
        btn.title = flagged ? 'Flagged — in the mod queue' : 'Flag for moderator review';
        var svg = btn.querySelector('svg');
        if (svg) svg.setAttribute('fill', flagged ? 'currentColor' : 'none');
      }
    }).catch(function () { if (btn) btn.disabled = false; });
  });

  /* ---- collapse / reply toggles ---- */
  document.addEventListener('click', function (ev) {
    var t = ev.target.closest('[data-ctoggle]');
    if (!t) return;
    var card = t.closest('.comment');
    if (!card) return;
    var action = t.getAttribute('data-ctoggle');
    if (action === 'collapse') {
      var collapsed = card.classList.toggle('is-collapsed');
      t.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      t.textContent = collapsed ? 'Expand' : 'Collapse';
    } else if (action === 'reply') {
      var rf = card.querySelector(':scope > .c-reply-form');
      if (rf) {
        rf.hidden = !rf.hidden;
        t.setAttribute('aria-expanded', rf.hidden ? 'false' : 'true');
        var ta = rf.querySelector('textarea');
        if (ta && !rf.hidden) ta.focus();
      }
    } else if (action === 'edit') {
      var ef = card.querySelector(':scope > .c-edit-form');
      if (ef) {
        ef.hidden = !ef.hidden;
        var eta = ef.querySelector('textarea');
        if (eta && !ef.hidden) { eta.focus(); eta.setSelectionRange(eta.value.length, eta.value.length); }
      }
    }
  });

  /* ---- inline edit submit ---- */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form.classList || !form.classList.contains('cedit-form')) return;
    if (!signedIn()) return;
    ev.preventDefault();
    var card = form.closest('.comment');
    var ta = form.querySelector('textarea[name="body"]');
    var body = (ta.value || '').trim();
    if (!body) return;
    var btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    postJSON('/comment/edit', {
      csrf_token: csrfToken(),
      target_type: form.querySelector('[name="target_type"]').value,
      target_id: parseInt(form.querySelector('[name="target_id"]').value, 10),
      body: body
    }).then(function (res) {
      if (btn) btn.disabled = false;
      if (!res.body.ok || !card) return;
      var bodyEl = card.querySelector(':scope > .comment-body');
      if (bodyEl) bodyEl.textContent = body;
      var head = card.querySelector(':scope > .comment-head');
      if (head && !head.querySelector('.edited')) {
        var s = document.createElement('span');
        s.className = 'edited';
        s.textContent = '(edited)';
        head.appendChild(s);
      }
      form.closest('.c-edit-form').hidden = true;
    }).catch(function () { if (btn) btn.disabled = false; });
  });

  /* ---- relative-time hydration ----
   * Server renders reltime text; this keeps it fresh and converts any
   * data-ts timestamps the server left as ISO fallbacks. */
  function hydrateTimes(root) {
    (root || document).querySelectorAll('time[data-ts]').forEach(function (el) {
      if (el.dataset.hydrated) return;
      el.dataset.hydrated = '1';
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hydrateTimes(); });
  } else { hydrateTimes(); }

  window.MuseFMComments = {
    csrfToken: csrfToken,
    /* refreshVoteUI now delegates to the shared votes.js painter */
    refreshVoteUI: function (scope, myVote, score) {
      if (window.MuseFMVotes) window.MuseFMVotes.paint(scope, myVote, score);
    },
    hydrateTimes: hydrateTimes
  };
})();
