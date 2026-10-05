/* MuseFM — audio engine. One shared <audio>, Spotify-style player UI:
   collapsed mini-bar above the bottom nav, tap to expand a full sheet
   (artwork, scrubber, -15/+30, speed, sleep, share, up-next queue).
   Rebuilt 2026-10-05 (Anthony): the old floating glass card is gone. */
window.TSPlayer = (function () {
  var audio = new Audio();
  audio.preload = 'metadata';
  var state = {
    queue: [],            // [{slug, src, title, dur, art, series}]
    current: null,        // {slug, src, title, dur, art, series, el: audio}
    sleepTimer: null,
    sleepMin: 0,
    speedIdx: 0,
  };
  var SPEEDS = [1, 1.25, 1.5, 2, 0.75];
  var SLEEPS = [0, 15, 30, 60];

  var bar = document.getElementById('playerbar');
  var pbOpen = document.getElementById('pb-open');
  var pbArt = document.getElementById('pb-art');
  var pbTitle = document.getElementById('pb-title');
  var pbPlay = document.getElementById('pb-play');
  var sheet = document.getElementById('playersheet');
  var scrim = document.getElementById('ps-scrim');
  var psArt = document.getElementById('ps-art');
  var psTitle = document.getElementById('ps-title');
  var psSub = document.getElementById('ps-sub');
  var psElapsed = document.getElementById('ps-elapsed');
  var psTotal = document.getElementById('ps-total');
  var psScrub = document.getElementById('ps-scrub');
  var psFill = document.getElementById('ps-fill');
  var psPlay = document.getElementById('ps-play');
  var psSpeed = document.getElementById('ps-speed');
  var psSleep = document.getElementById('ps-sleep');

  // iOS Safari (and spec-compliant browsers) throw InvalidStateError if
  // currentTime is set while readyState is HAVE_NOTHING. The old code did
  // `audio.currentTime = 0` synchronously after `audio.src = ...`, which
  // aborted load() before play() ever ran — tapping Play did nothing.
  // Always seek after metadata is available instead.
  var loadSeq = 0;
  function seekSafe(t, seq) {
    if (audio.readyState >= 1) { try { audio.currentTime = t; } catch (e) {} return; }
    var once = function () {
      audio.removeEventListener('loadedmetadata', once);
      if (seq !== loadSeq) return; // superseded by a newer load()
      try { audio.currentTime = t; } catch (e2) {}
    };
    audio.addEventListener('loadedmetadata', once);
  }

  function fmt(s) {
    if (!isFinite(s)) return '0:00';
    s = Math.floor(s);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }

  function showBar() {
    // Player markup is optional: if a page ever renders without it,
    // the audio still plays — only the sticky UI is skipped.
    if (!bar) return;
    bar.classList.add('show'); bar.setAttribute('aria-hidden', 'false');
    document.body.classList.add('has-player');
  }
  function hideBar() {
    if (!bar) return;
    bar.classList.remove('show'); bar.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('has-player');
    closeSheet();
  }
  function openSheet() {
    if (!sheet || !state.current) return;
    sheet.classList.add('show'); sheet.setAttribute('aria-hidden', 'false');
    if (scrim) { scrim.hidden = false; requestAnimationFrame(function () { scrim.classList.add('show'); }); }
  }
  function closeSheet() {
    if (!sheet) return;
    sheet.classList.remove('show'); sheet.setAttribute('aria-hidden', 'true');
    if (scrim) { scrim.classList.remove('show'); scrim.hidden = true; }
  }
  function sheetOpen() { return sheet && sheet.classList.contains('show'); }

  function setArtwork(item) {
    var art = item.art || '';
    if (pbArt) {
      if (art) pbArt.innerHTML = '<img class="pb-art" src="' + escapeHtml(art) + '" alt="">';
      else pbArt.innerHTML = '&#9834;';
    }
    if (psArt) {
      if (art) { psArt.src = art; psArt.style.display = ''; }
      else { psArt.removeAttribute('src'); psArt.style.display = 'none'; }
    }
  }

  function load(item, autoplay, startAt) {
    state.current = Object.assign({ el: audio }, item);
    var seq = ++loadSeq;
    audio.src = item.src;
    if (pbTitle) pbTitle.textContent = item.title || '…';
    if (psTitle) psTitle.textContent = item.title || '…';
    if (psSub) psSub.textContent = item.series ? 'MuseFM · ' + item.series : 'MuseFM';
    setArtwork(item);
    showBar();
    renderQueue();
    seekSafe(startAt || 0, seq);
    if (autoplay) {
      var p = audio.play();
      if (p) p.catch(function () { toast('Could not play — tap again'); });
    }
    updatePlayBtn();
  }

  function playItem(item, startAt) {
    // pull from queue if it matches, else just play
    load(item, true, startAt || 0);
  }

  function enqueue(item) {
    state.queue.push(item);
    if (!state.current) playItem(state.queue.shift());
    else { renderQueue(); toast('Added to up next'); }
    showBar();
  }

  function next() {
    if (state.queue.length) playItem(state.queue.shift());
    else { audio.pause(); updatePlayBtn(); }
  }

  function toggle() {
    if (!state.current) return;
    if (audio.paused) { var p = audio.play(); if (p) p.catch(function () { toast('Could not play — tap again'); }); }
    else audio.pause();
    updatePlayBtn();
  }

  function updatePlayBtn() {
    var icon = audio.paused ? '▶' : '⏸';
    if (pbPlay) { pbPlay.innerHTML = icon === '▶' ? '&#9654;' : '&#10074;&#10074;'; pbPlay.setAttribute('aria-label', audio.paused ? 'Play' : 'Pause'); }
    if (psPlay) { psPlay.innerHTML = icon === '▶' ? '&#9654;' : '&#10074;&#10074;'; psPlay.setAttribute('aria-label', audio.paused ? 'Play' : 'Pause'); }
  }

  function paintProgress() {
    var d = audio.duration || (state.current && state.current.dur) || 0;
    var c = audio.currentTime || 0;
    if (psElapsed) psElapsed.textContent = fmt(c);
    if (psTotal) psTotal.textContent = fmt(d);
    if (d && psFill) psFill.style.width = (100 * c / d) + '%';
    if (psScrub && d) psScrub.setAttribute('aria-valuenow', String(Math.round(100 * c / d)));
  }

  audio.addEventListener('play', updatePlayBtn);
  audio.addEventListener('pause', updatePlayBtn);
  audio.addEventListener('ended', next);
  audio.addEventListener('timeupdate', paintProgress);
  audio.addEventListener('loadedmetadata', paintProgress);

  function scrubTo(clientX) {
    if (!psScrub) return;
    var r = psScrub.getBoundingClientRect();
    var d = audio.duration || (state.current && state.current.dur);
    if (!d) return;
    var ratio = (clientX - r.left) / r.width;
    ratio = Math.max(0, Math.min(1, ratio));
    try { audio.currentTime = d * ratio; } catch (e) {}
    paintProgress();
  }
  if (psScrub) {
    var scrubbing = false;
    psScrub.addEventListener('pointerdown', function (e) {
      scrubbing = true;
      try { psScrub.setPointerCapture(e.pointerId); } catch (err) {}
      scrubTo(e.clientX);
    });
    psScrub.addEventListener('pointermove', function (e) { if (scrubbing) scrubTo(e.clientX); });
    psScrub.addEventListener('pointerup', function () { scrubbing = false; });
    psScrub.addEventListener('pointercancel', function () { scrubbing = false; });
    psScrub.addEventListener('keydown', function (e) {
      var d = audio.duration || 0;
      if (e.key === 'ArrowLeft') { try { audio.currentTime = Math.max(0, audio.currentTime - 5); } catch (err) {} e.preventDefault(); }
      else if (e.key === 'ArrowRight') { try { audio.currentTime = Math.min(d, audio.currentTime + 5); } catch (err) {} e.preventDefault(); }
    });
  }

  // Episode-row click delegation is wired FIRST, before any player
  // control wiring: a missing player element must never silently kill
  // episode playback (previously `mpPlay.onclick = toggle` threw on a null
  // lookup and the delegation below never registered — tapping Play did
  // nothing anywhere on the page).
  document.addEventListener('click', function (e) {
    var p = e.target.closest('[data-play]');
    if (p) {
      // Rows with their own <audio> element are driven by audio-bar.js
      // (Jams/Jabs member rows, the /jabs featured card). The global
      // player must not also fire on them: its item has no src, which
      // would toast a spurious "could not play" error. (2026-10-05)
      var owned = p.closest('.glass-track');
      if (owned && owned.querySelector('[data-audio]')) return;
      var it = itemFrom(p);
      var start = parseInt(p.getAttribute('data-start') || '0', 10);
      playItem(it, start);
      return;
    }
    var q = e.target.closest('[data-queue]');
    if (q) enqueue(itemFrom(q));
  });

  // controls — each lookup guarded so a page without the player
  // markup can't throw here either.
  function on(id, ev, fn) {
    var el = document.getElementById(id);
    if (el) el[ev] = fn;
  }
  on('pb-play', 'onclick', function (e) { e.stopPropagation(); toggle(); });
  on('ps-play', 'onclick', toggle);
  on('pb-open', 'onclick', openSheet);
  on('ps-collapse', 'onclick', closeSheet);
  on('ps-scrim', 'onclick', closeSheet);
  on('ps-dismiss', 'onclick', function () {
    audio.pause(); state.current = null; state.queue = [];
    renderQueue(); hideBar(); updatePlayBtn();
  });
  on('ps-rew', 'onclick', function () { try { audio.currentTime = Math.max(0, audio.currentTime - 15); } catch (e) {} });
  on('ps-fwd', 'onclick', function () { try { audio.currentTime += 30; } catch (e) {} });
  on('ps-speed', 'onclick', function () {
    state.speedIdx = (state.speedIdx + 1) % SPEEDS.length;
    var v = SPEEDS[state.speedIdx];
    audio.playbackRate = v;
    if (psSpeed) psSpeed.textContent = String(v).replace(/\.0$/, '') + '×';
  });
  on('ps-sleep', 'onclick', function () {
    if (state.sleepTimer) { clearTimeout(state.sleepTimer); state.sleepTimer = null; }
    var i = (SLEEPS.indexOf(state.sleepMin) + 1) % SLEEPS.length;
    state.sleepMin = SLEEPS[i];
    if (state.sleepMin > 0) {
      state.sleepTimer = setTimeout(function () {
        audio.pause(); updatePlayBtn(); toast('Sleep timer — goodnight');
      }, state.sleepMin * 60000);
      toast('Sleep in ' + state.sleepMin + ' min');
    }
    if (psSleep) {
      psSleep.textContent = state.sleepMin ? state.sleepMin + 'm' : 'Sleep off';
      psSleep.classList.toggle('on', !!state.sleepMin);
    }
  });
  on('ps-share', 'onclick', function () {
    if (!state.current) return;
    if (typeof openShare === 'function') openShare(state.current.slug, state.current.title);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && sheetOpen()) closeSheet();
  });

  function renderQueue() {
    var q = document.getElementById('ps-queue');
    if (!q) return;
    if (!state.queue.length) { q.innerHTML = ''; return; }
    var html = '<div class="ps-qhead">UP NEXT</div>';
    state.queue.forEach(function (it, i) {
      html += '<div class="q-item"><span>' + escapeHtml(it.title) + '</span>' +
        '<button data-qrm="' + i + '" aria-label="Remove from up next">✕</button></div>';
    });
    q.innerHTML = html;
    q.querySelectorAll('[data-qrm]').forEach(function (b) {
      b.onclick = function () { state.queue.splice(parseInt(b.getAttribute('data-qrm'), 10), 1); renderQueue(); };
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // wire episode rows
  function itemFrom(el) {
    return {
      slug: el.getAttribute('data-play') || el.getAttribute('data-queue'),
      src: el.getAttribute('data-src'),
      title: el.getAttribute('data-title'),
      dur: parseInt(el.getAttribute('data-dur') || '0', 10),
      art: el.getAttribute('data-art'),
      series: el.getAttribute('data-series'),
    };
  }
  // (click delegation lives above, wired before the player controls)

  // #t= deep links: /episodes#slug?t=90
  function deepLink() {
    var h = location.hash; // #slug?t=90
    if (!h || h.length < 2) return;
    var m = h.slice(1).match(/^([a-z0-9-]+)(\?t=(\d+))?$/);
    if (!m) return;
    var btn = document.querySelector('[data-play="' + m[1] + '"]');
    if (btn) {
      var it = itemFrom(btn);
      playItem(it, m[3] ? parseInt(m[3], 10) : 0);
      var card = document.getElementById(m[1]);
      if (card) setTimeout(function () { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); }, 300);
    }
  }
  window.addEventListener('hashchange', deepLink);
  document.addEventListener('DOMContentLoaded', deepLink);

  return { enqueue: enqueue, playItem: playItem,
           get current() { return state.current; } };
})();
