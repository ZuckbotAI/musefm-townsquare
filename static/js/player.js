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

  function wireRowButtons() {
    document.querySelectorAll('[data-play]').forEach(function (btn) {
      if (btn._tsWired) return; btn._tsWired = true;
      btn.addEventListener('click', function (e) {
        e.preventDefault(); e.stopPropagation();
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
