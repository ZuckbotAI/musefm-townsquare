/* MuseFM — async votes with immediate feedback, lock, and rollback.
   One delegated submit handler for every form POSTing to /vote: feed and
   community post votes (.vote-form), comment votes (.cvote-form, including
   the ones built by JS in the Shorts panels and the homepage mini reel).
   The plain form POST stays as the no-JS fallback, and for signed-out
   visitors the server still redirects to /login.

   Behavior per tap:
   - immediate feedback: the button state and score update instantly
     (optimistic, using the same toggle semantics as the server: tapping
     the active side clears the vote).
   - lock: while a request is in flight for a target, further taps on
     that target are ignored and both buttons disable (.is-voting).
   - rollback: on any failure the previous state is restored and a toast
     explains what happened. Server truth always wins on success. */
(function () {
  'use strict';

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    if (m && m.content) return m.content;
    var inp = document.querySelector('input[name="csrf_token"]');
    return inp ? inp.value : '';
  }
  function signedIn() { return !!csrfToken(); }

  function toast(msg) {
    if (window.fmToast) { window.fmToast(msg); return; }
    if (typeof window.toast === 'function') { window.toast(msg); return; }
    if (window.console) console.log('[votes] ' + msg);
  }

  /* The scope owns both buttons and the score: .cvote (comments) or
     .votes (feed / community post rows). */
  function scopeOf(form) {
    return form.closest('.cvote, .votes');
  }
  function upBtn(scope) {
    return scope.querySelector('.cvote-btn.up, .vote-btn.up');
  }
  function downBtn(scope) {
    return scope.querySelector('.cvote-btn.down, .vote-btn.down');
  }
  function scoreEl(scope) {
    return scope.querySelector('.cvote-score, .vote-score');
  }

  function readState(scope) {
    var s = scoreEl(scope);
    return {
      myVote: parseInt(scope.getAttribute('data-my-vote') || '0', 10) || 0,
      score: s ? (parseInt(s.textContent, 10) || 0) : 0
    };
  }

  function paint(scope, myVote, score) {
    var up = upBtn(scope), down = downBtn(scope), s = scoreEl(scope);
    scope.setAttribute('data-my-vote', String(myVote));
    if (up) {
      up.classList.toggle('is-active', myVote === 1);
      up.setAttribute('aria-pressed', myVote === 1 ? 'true' : 'false');
    }
    if (down) {
      down.classList.toggle('is-active', myVote === -1);
      down.setAttribute('aria-pressed', myVote === -1 ? 'true' : 'false');
    }
    if (s && score != null) {
      s.textContent = score;
      s.setAttribute('aria-label', 'Score ' + score);
      s.classList.toggle('pos', score > 0);
      s.classList.toggle('neg', score < 0);
    }
  }

  function setLocked(scope, locked) {
    scope.classList.toggle('is-voting', locked);
    [upBtn(scope), downBtn(scope)].forEach(function (b) {
      if (b) b.disabled = locked;
    });
  }

  function flashError(scope) {
    scope.classList.add('vote-error');
    setTimeout(function () { scope.classList.remove('vote-error'); }, 650);
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().then(function (d) { return { status: r.status, body: d }; });
    });
  }

  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (!form || !form.classList || !form.getAttribute) return;
    var action;
    try {
      action = new URL(form.action, window.location.origin).pathname;
    } catch (e) { return; }
    if (action !== '/vote') return;
    if (!signedIn()) return; /* plain POST: server redirects to /login */
    var scope = scopeOf(form);
    if (!scope) return;
    ev.preventDefault();
    if (scope._voting) return; /* lock: one flight per target at a time */
    var valInput = form.querySelector('[name="value"]');
    var typeInput = form.querySelector('[name="target_type"]');
    var idInput = form.querySelector('[name="target_id"]');
    if (!valInput || !typeInput || !idInput) return;
    var val = parseInt(valInput.value, 10);
    if (val !== 1 && val !== -1) return;

    var prev = readState(scope);
    /* Optimistic: toggle semantics match db.vote — tapping the active
       side clears the vote. */
    var next = (prev.myVote === val) ? 0 : val;
    scope._voting = true;
    setLocked(scope, true);
    paint(scope, next, prev.score + (next - prev.myVote));

    postJSON('/vote', {
      csrf_token: csrfToken(),
      target_type: typeInput.value,
      target_id: parseInt(idInput.value, 10),
      value: val
    }).then(function (res) {
      scope._voting = false;
      setLocked(scope, false);
      var d = res.body || {};
      if (res.status === 401 && d.signin_url) {
        window.location.href = d.signin_url;
        return;
      }
      if (!d.ok) {
        paint(scope, prev.myVote, prev.score); /* rollback */
        flashError(scope);
        toast(d.error || 'vote failed — try again');
        return;
      }
      /* Server truth wins over the optimistic guess. */
      var mv = (d.my_vote === 1 || d.my_vote === -1) ? d.my_vote : 0;
      paint(scope, mv, d.score);
    }).catch(function () {
      scope._voting = false;
      setLocked(scope, false);
      paint(scope, prev.myVote, prev.score); /* rollback */
      flashError(scope);
      toast('network hiccup — vote was not counted, try again');
    });
  });

  /* Exposed for tests and dynamically built widgets. */
  window.MuseFMVotes = { paint: paint, readState: readState };
})();
