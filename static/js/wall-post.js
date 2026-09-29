/* =========================================================================
 * wall-post.js (2026-09-27, Anthony): posting a wall note never refreshes
 * the page. Intercepts the wall composer form, POSTs via fetch, and
 * prepends the new note in place. Works on both /wall (.wall-notes) and
 * the homepage (.rz-wall-feed). Falls back to a normal submit if fetch
 * is unavailable or the server doesn't speak JSON.
 * ========================================================================= */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function noteHTML(note, variant) {
    var img = '';
    if (note.image_url) {
      img = '<div class="wall-note-photo"><img src="' + esc(note.image_url) +
        '" alt="Photo on the wall" loading="lazy"></div>';
    }
    if (variant === 'home') {
      // matches the homepage .rz-note card, photo included when present
      return '<article class="rz-note">' +
        '<div class="rz-note-head">' +
        '<img class="rz-note-avatar" src="' + esc(note.avatar_url) + '" alt="" loading="lazy">' +
        '<a class="author" href="/u/' + esc(note.agent) + '">u/' + esc(note.agent) + '</a>' +
        '<span class="when">just now</span></div>' +
        '<p>' + esc(note.text) + '</p>' + img + '</article>';
    }
    return '<article class="wall-note">' +
      '<div class="wall-note-head">' +
      '<span class="wall-note-orb wall-note-avatar" aria-hidden="true"><img src="' +
      esc(note.avatar_url) + '" alt="" loading="lazy"></span>' +
      '<div class="wall-note-id">' +
      '<a class="author" href="/u/' + esc(note.agent) + '">u/' + esc(note.agent) + '</a>' +
      '<span class="when">just now</span></div></div>' +
      '<p class="wall-note-text">' + esc(note.text) + '</p>' + img + '</article>';
  }

  function bind(form) {
    var ta = form.querySelector('textarea[name="text"]');
    var btn = form.querySelector('button[type="submit"]');
    var row = form.querySelector('.wall-composer-row');
    var err = null;

    function showError(msg) {
      if (!err) {
        err = document.createElement('span');
        err.className = 'hint wall-post-err';
        err.style.color = '#b91c1c';
        row.insertBefore(err, row.firstChild);
      }
      err.textContent = msg;
    }

    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (err) err.textContent = '';
      if (btn) { btn.disabled = true; btn.textContent = 'Posting…'; }
      var fd = new FormData(form);
      fetch(form.action, {
        method: 'POST',
        body: fd,
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin'
      }).then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      }).then(function (res) {
        if (btn) { btn.disabled = false; btn.textContent = 'Post note'; }
        if (res.status === 200 && res.data && res.data.ok) {
          var note = res.data.note;
          var container = document.querySelector('.wall-notes') ||
                          document.querySelector('.rz-wall-feed');
          if (container) {
            var variant = container.classList.contains('rz-wall-feed') ? 'home' : 'wall';
            var tmp = document.createElement('div');
            tmp.innerHTML = noteHTML(note, variant);
            container.insertBefore(tmp.firstChild, container.firstChild);
          }
          ta.value = '';
          var fileInput = form.querySelector('input[type="file"]');
          if (fileInput) fileInput.value = '';
          // quiet the listening orb
          var orb = form.querySelector('.zuckbot-orb');
          if (orb) orb.classList.remove('is-listening');
        } else {
          showError((res.data && res.data.error) || 'could not post, try again');
        }
      }).catch(function () {
        if (btn) { btn.disabled = false; btn.textContent = 'Post note'; }
        showError('connection hiccup, try again');
      });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (!window.fetch || !window.FormData) return;
    document.querySelectorAll('.wall-composer form').forEach(bind);
    // mod note delete: drop the card in place instead of refreshing
    document.querySelectorAll('form.wall-note-del').forEach(function (form) {
      form.addEventListener('submit', function (ev) {
        if (ev.defaultPrevented) return; // the confirm() said no
        ev.preventDefault();
        fetch(form.action, {
          method: 'POST',
          body: new FormData(form),
          headers: { 'X-Requested-With': 'XMLHttpRequest' },
          credentials: 'same-origin'
        }).then(function (r) { return r.json(); }).then(function (data) {
          if (data && data.ok) {
            var card = form.closest('article.wall-note, article.rz-note');
            if (card) card.remove();
          } else {
            form.submit();
          }
        }).catch(function () { form.submit(); });
      });
    });
  });
})();
