/* MuseFM — audio engine + floating glass player card.
   Rebuilt 2026-10-05 (Anthony: restore the glass player).
   One shared <audio>, frosted glass bar with artwork, title, scrubber,
   -15 / play-pause / +15 / volume. Driven via the gp- ids. */
window.TSPlayer = (function () {
  var audio = new Audio();
  audio.preload = 'metadata';
  var state = {
    queue: [],
    current: null,
  };

  var bar = document.getElementById('playerbar');
  var gpArt = document.getElementById('gp-art');
  var gpTitle = document.getElementById('gp-title');
  var gpSub = document.getElementById('gp-sub');
  var gpScrub = document.getElementById('gp-scrub');
  var gpFill = document.getElementById('gp-fill');
  var gpDot = document.getElementById('gp-dot');
  var gpElapsed = document.getElementById('gp-elapsed');
  var gpTotal = document.getElementById('gp-total');
  var gpPlay = document.getElementById('gp-play');
  var gpRew = document.getElementById('gp-rew');
  var gpFwd = document.getElementById('gp-fwd');
  var gpVol = document.getElementById('gp-vol');
  var gpClose = document.getElementById('gp-close');

  var loadSeq = 0;
  function seekSafe(t, seq) {
    if (audio.readyState >= 1) { try { audio.currentTime = t; } catch (e) {} return; }
    var once = function () {
      audio.removeEventListener('loadedmetadata', once);
      if (seq !== loadSeq) return;
      try { audio.currentTime = t; } catch (e2) {}
    };
    audio.addEventListener('loadedmetadata', once);
  }

  function fmt(s) {
    if (!isFinite(s)) return '0:00';
    s = Math.floor(s);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function showBar() {
    if (!bar) return;
    bar.classList.add('show'); bar.setAttribute('aria-hidden', 'false');
    document.body.classList.add('has-player');
  }
  function hideBar() {
    if (!bar) return;
    bar.classList.remove('show'); bar.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('has-player');
  }

  function setArtwork(item) {
    var art = item.art || '';
    if (!gpArt) return;
    if (art) gpArt.innerHTML = '<img src="' + escapeHtml(art) + '" alt="">';
    else gpArt.innerHTML = '<span>&#9834;</span>';
  }

  function setMediaSession(item) {
    // iOS Control Center / lock screen: show episode info instead of "Not Playing".
    if (!('mediaSession' in navigator)) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: item.title || 'MuseFM',
        artist: item.series ? 'MuseFM · ' + item.series : 'MuseFM',
        album: 'MuseFM',
        artwork: item.art ? [{ src: item.art, sizes: '512x512', type: 'image/jpeg' }] : []
      });
      navigator.mediaSession.setActionHandler('play', function () { toggle(); });
      navigator.mediaSession.setActionHandler('pause', function () { toggle(); });
      navigator.mediaSession.setActionHandler('previoustrack', function () {
        try { audio.currentTime = Math.max(0, audio.currentTime - 15); } catch (e) {}
      });
      navigator.mediaSession.setActionHandler('nexttrack', function () {
        try { audio.currentTime = Math.min(audio.duration || 1e9, audio.currentTime + 15); } catch (e) {}
      });
    } catch (e) {}
  }

  function load(item, autoplay, startAt) {
    state.current = Object.assign({ el: audio }, item);
    var seq = ++loadSeq;
    audio.src = item.src;
    if (gpTitle) gpTitle.textContent = item.title || '…';
    if (gpSub) gpSub.textContent = item.series ? 'MuseFM · ' + item.series : (item.episode ? 'Episode ' + item.episode : 'MuseFM');
    setArtwork(item);
    setMediaSession(item);
    showBar();
    seekSafe(startAt || 0, seq);
    if (autoplay) {
      var p = audio.play();
      if (p) p.catch(function () {});
    }
    updatePlayBtn();
  }

  function toggle() {
    if (!state.current) return;
    if (audio.paused) { var p = audio.play(); if (p) p.catch(function () {}); }
    else audio.pause();
    updatePlayBtn();
  }

  function updatePlayBtn() {
    if (!gpPlay) return;
    var playing = !audio.paused;
    gpPlay.innerHTML = playing ? '&#10074;&#10074;' : '&#9654;';
    gpPlay.setAttribute('aria-label', playing ? 'Pause' : 'Play');
  }

  function paintProgress() {
    var d = audio.duration || (state.current && state.current.dur) || 0;
    var c = audio.currentTime || 0;
    if (gpElapsed) gpElapsed.textContent = fmt(c);
    if (gpTotal) gpTotal.textContent = fmt(d);
    var pct = d ? (100 * c / d) : 0;
    if (gpFill) gpFill.style.width = pct + '%';
    if (gpDot) gpDot.style.left = pct + '%';
    if (gpScrub && d) gpScrub.setAttribute('aria-valuenow', String(Math.round(pct)));
    // Keep iOS lock screen progress in sync.
    if ('mediaSession' in navigator && d) {
      try { navigator.mediaSession.setPositionState({ duration: d, playbackRate: audio.playbackRate || 1, position: c }); } catch (e) {}
    }
  }

  function scrubTo(clientX) {
    if (!gpScrub || !state.current) return;
    var r = gpScrub.getBoundingClientRect();
    var ratio = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    var d = audio.duration || state.current.dur || 0;
    if (d) { seekSafe(ratio * d, loadSeq); paintProgress(); }
  }

  audio.addEventListener('play', updatePlayBtn);
  audio.addEventListener('pause', updatePlayBtn);
  audio.addEventListener('ended', function () {
    if (state.queue.length) load(state.queue.shift(), true, 0);
    else { updatePlayBtn(); }
  });
  audio.addEventListener('timeupdate', paintProgress);
  audio.addEventListener('loadedmetadata', paintProgress);

  if (gpPlay) gpPlay.addEventListener('click', function (e) { e.stopPropagation(); toggle(); });
  if (gpRew) gpRew.addEventListener('click', function (e) {
    e.stopPropagation();
    try { audio.currentTime = Math.max(0, audio.currentTime - 15); } catch (err) {}
  });
  if (gpFwd) gpFwd.addEventListener('click', function (e) {
    e.stopPropagation();
    try { audio.currentTime = Math.min(audio.duration || 1e9, audio.currentTime + 15); } catch (err) {}
  });
  if (gpVol) gpVol.addEventListener('click', function (e) {
    e.stopPropagation();
    audio.muted = !audio.muted;
    gpVol.classList.toggle('muted', audio.muted);
  });
  if (gpClose) gpClose.addEventListener('click', function (e) {
    e.stopPropagation();
    audio.pause(); audio.removeAttribute('src'); audio.load();
    state.current = null; state.queue = [];
    hideBar(); updatePlayBtn();
  });
  if (gpScrub) {
    gpScrub.addEventListener('pointerdown', function (e) {
      scrubTo(e.clientX);
      var move = function (ev) { scrubTo(ev.clientX); };
      var up = function () {
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    });
    gpScrub.addEventListener('keydown', function (e) {
      var d = audio.duration || 0; if (!d) return;
      if (e.key === 'ArrowLeft') { try { audio.currentTime = Math.max(0, audio.currentTime - 5); } catch (err) {} }
      if (e.key === 'ArrowRight') { try { audio.currentTime = Math.min(d, audio.currentTime + 5); } catch (err) {} }
    });
  }

  // Public API: wire up [data-play] buttons on episode rows.
  // Single audio controller: pause ALL other <audio> elements on the page
  // before playing, so we never get overlapping playback.
  function pauseAllOthers() {
    document.querySelectorAll('audio').forEach(function (el) {
      if (el !== audio && !el.paused) { try { el.pause(); } catch (e) {} }
    });
  }

  function setRowButtonState(activeBtn, playing) {
    // Reset all track buttons to play state, then set the active one.
    document.querySelectorAll('[data-play]').forEach(function (b) {
      b.innerHTML = '&#9654;';
      b.setAttribute('aria-label', 'Play');
      b.classList.remove('is-playing');
    });
    if (activeBtn && playing) {
      activeBtn.innerHTML = '&#10074;&#10074;';
      activeBtn.setAttribute('aria-label', 'Pause');
      activeBtn.classList.add('is-playing');
    }
    state.activeBtn = playing ? activeBtn : null;
  }

  // When the global audio pauses/ends, reset the track button.
  audio.addEventListener('pause', function () {
    if (state.activeBtn) setRowButtonState(null, false);
  });

  function wireRowButtons() {
    document.querySelectorAll('[data-play]').forEach(function (btn) {
      if (btn._tsWired) return; btn._tsWired = true;
      btn.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
        // If this track is already playing, toggle pause.
        if (state.activeBtn === btn && !audio.paused) {
          audio.pause();
          return;
        }
        pauseAllOthers();
        var row = btn.closest('[data-track]');
        var audioEl = row ? row.querySelector('[data-audio]') : null;
        var src = audioEl ? (audioEl.getAttribute('src') || audioEl.querySelector('source')?.getAttribute('src')) : null;
        if (!src) return;
        var titleEl = row ? row.querySelector('.glass-title2, .ep-title') : null;
        var title = titleEl ? titleEl.textContent.trim() : 'MuseFM';
        var artImg = row ? row.querySelector('.glass-art-sq img, .ep-art img') : null;
        var art = artImg ? artImg.getAttribute('src') : '';
        // If tapping the currently-playing track, just toggle.
        if (state.current && state.current.src === src) { toggle(); return; }
        load({ src: src, title: title, art: art, series: '', episode: '' }, true, 0);
      });
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireRowButtons);
  } else { wireRowButtons(); }
  // Re-wire after dynamic content loads.
  new MutationObserver(function () { wireRowButtons(); }).observe(document.body, { childList: true, subtree: true });

  return { load: load, toggle: toggle, enqueue: function (item) {
    state.queue.push(item);
    if (!state.current) load(state.queue.shift(), true, 0);
    showBar();
  } };
})();

/* Track comment threads + flag links (2026-10-06, Anthony: fix member jab comments).
 * Moved here from audio-bar.js, which is no longer loaded anywhere. player.js
 * is the single audio controller, so the per-track [data-ctoggle]/[data-comments]
 * panels and [data-flag] links wire up here. Purely additive: touches nothing above. */
(function () {
  'use strict';

  function wireTrack(track) {
    if (track._tsCommentsWired) return; track._tsCommentsWired = true;

    // Comments: per-track thread panel, fetched on first open.
    var ctoggle = track.querySelector('[data-ctoggle]');
    var cpanel = track.querySelector('[data-comments]');
    var ccount = track.querySelector('[data-ccount]');
    if (ctoggle && cpanel) {
      var cuid = ctoggle.getAttribute('data-ctoggle');
      var cloaded = false;
      var copen = false;
      var csigned = !!document.querySelector('meta[name="csrf-token"]');

      function timeAgo(ts) {
        var s = Math.max(0, Math.floor(Date.now() / 1000) - (ts || 0));
        if (s < 60) return 'just now';
        if (s < 3600) { var m = Math.floor(s / 60); return m + 'm ago'; }
        if (s < 86400) { var h = Math.floor(s / 3600); return h + 'h ago'; }
        var d = Math.floor(s / 86400);
        return d + (d === 1 ? ' day ago' : ' days ago');
      }

      function showError(msg) {
        var e = cpanel.querySelector('.gc-error');
        if (!e) {
          e = document.createElement('div');
          e.className = 'gc-error';
          cpanel.insertBefore(e, cpanel.firstChild);
        }
        e.textContent = msg;
      }

      function commentNode(c, depth) {
        var wrap = document.createElement('div');
        wrap.className = 'gc-item';
        var head = document.createElement('div');
        head.className = 'gc-head';
        var b = document.createElement('b');
        b.textContent = '@' + c.handle;
        var t = document.createElement('span');
        t.textContent = ' ' + timeAgo(c.created_at);
        head.appendChild(b); head.appendChild(t);
        var body = document.createElement('div');
        body.className = 'gc-body';
        body.innerHTML = c.body_html || '';
        wrap.appendChild(head);
        wrap.appendChild(body);
        if (csigned && depth < 4) {
          var acts = document.createElement('div');
          acts.className = 'gc-actions';
          var rb = document.createElement('button');
          rb.className = 'gc-reply';
          rb.type = 'button';
          rb.textContent = 'Reply';
          rb.addEventListener('click', function () {
            toggleReplyForm(wrap, c.id);
          });
          acts.appendChild(rb);
          wrap.appendChild(acts);
        }
        if (c.replies && c.replies.length) {
          var reps = document.createElement('div');
          reps.className = 'gc-replies';
          c.replies.forEach(function (r) {
            reps.appendChild(commentNode(r, depth + 1));
          });
          wrap.appendChild(reps);
        }
        return wrap;
      }

      function commentForm(parentId, onDone) {
        var form = document.createElement('form');
        form.className = 'gc-form';
        var input = document.createElement('input');
        input.className = 'gc-input';
        input.name = 'body';
        input.maxLength = 2000;
        input.placeholder = parentId ? 'Write a reply…' : 'Add a comment…';
        input.setAttribute('autocomplete', 'off');
        input.setAttribute('aria-label', parentId ? 'Write a reply' : 'Add a comment');
        var send = document.createElement('button');
        send.className = 'gc-send';
        send.type = 'submit';
        send.textContent = 'Post';
        form.appendChild(input);
        form.appendChild(send);
        form.addEventListener('submit', function (ev) {
          ev.preventDefault();
          var body = input.value.trim();
          if (!body) return;
          send.disabled = true;
          var meta = document.querySelector('meta[name="csrf-token"]');
          fetch('/api/audio/' + cuid + '/comments', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              parent_id: parentId,
              body: body,
              csrf_token: meta ? meta.content : ''
            })
          }).then(function (r) { return r.json(); }).then(function (j) {
            send.disabled = false;
            if (j && j.ok) {
              if (typeof j.comment_count === 'number' && ccount) {
                ccount.textContent = j.comment_count;
              }
              loadThread();
              if (onDone) onDone();
            } else if (j && j.signin_url) {
              window.location.href = j.signin_url;
            } else {
              showError((j && j.error) || 'Could not post, try again.');
            }
          }).catch(function () {
            send.disabled = false;
            showError('Could not post, try again.');
          });
        });
        return form;
      }

      function toggleReplyForm(wrap, parentId) {
        var old = wrap.querySelector(':scope > .gc-form');
        if (old) { old.remove(); return; }
        wrap.appendChild(commentForm(parentId, function () {
          var f = wrap.querySelector(':scope > .gc-form');
          if (f) f.remove();
        }));
        var inp = wrap.querySelector(':scope > .gc-form .gc-input');
        if (inp) inp.focus();
      }

      function renderThread(list) {
        cpanel.innerHTML = '';
        if (csigned) {
          cpanel.appendChild(commentForm(null, null));
        } else {
          var n = document.createElement('div');
          n.className = 'gc-signin';
          var a = document.createElement('a');
          a.href = '/login?next=' + encodeURIComponent(window.location.pathname);
          a.textContent = 'Sign in to comment';
          n.appendChild(a);
          cpanel.appendChild(n);
        }
        if (!list.length) {
          var e = document.createElement('div');
          e.className = 'gc-empty';
          e.textContent = 'No comments yet. Say something kind.';
          cpanel.appendChild(e);
          return;
        }
        list.forEach(function (c) {
          cpanel.appendChild(commentNode(c, 0));
        });
      }

      function loadThread() {
        fetch('/api/audio/' + cuid + '/comments?limit=20')
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (j && j.ok) {
              renderThread(j.comments || []);
              if (typeof j.count === 'number' && ccount) {
                ccount.textContent = j.count;
              }
            } else {
              showError('Could not load comments.');
            }
          })
          .catch(function () { showError('Could not load comments.'); });
      }

      ctoggle.addEventListener('click', function () {
        copen = !copen;
        ctoggle.setAttribute('aria-expanded', copen ? 'true' : 'false');
        if (copen) {
          cpanel.hidden = false;
          if (!cloaded) { cloaded = true; loadThread(); }
        } else {
          cpanel.hidden = true;
        }
      });
    }

    // Flag link: one tap sends target_type=upload to /flag (JSON).
    var flag = track.querySelector('[data-flag]');
    if (flag) {
      flag.addEventListener('click', function () {
        if (flag.classList.contains('done')) return;
        var meta = document.querySelector('meta[name="csrf-token"]');
        fetch('/flag', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            target_type: 'upload',
            target_id: parseInt(flag.getAttribute('data-flag'), 10),
            reason: 'other',
            csrf_token: meta ? meta.content : ''
          })
        }).then(function (r) { return r.json(); }).then(function (j) {
          if (j && (j.ok || j.flagged)) {
            flag.classList.add('done');
            flag.textContent = 'flagged, thanks';
          }
        }).catch(function () { /* leave the link in place */ });
      });
    }
  }

  function wireAll() {
    document.querySelectorAll('[data-track]').forEach(wireTrack);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireAll);
  } else { wireAll(); }
  // Re-wire after dynamic content loads.
  new MutationObserver(function () { wireAll(); }).observe(document.body, { childList: true, subtree: true });
})();
