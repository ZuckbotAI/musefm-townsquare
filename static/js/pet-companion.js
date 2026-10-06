/* Site-wide pet companion (2026-10-06)
   Uses the real pet model from the server. Tap to pat, tap again for quick-care.
   All care actions hit the real web endpoints with CSRF. */
(function() {
  'use strict';

  var companion = document.getElementById('pet-companion');
  if (!companion) return; // No pet or not logged in

  var panel = document.getElementById('pc-panel');
  var art = companion.querySelector('.pc-art');
  var csrfToken = null;
  var meta = document.querySelector('meta[name="csrf-token"]');
  if (meta) csrfToken = meta.getAttribute('content');

  var panelOpen = false;
  var lastTap = 0;

  // Idle bob is handled by CSS class
  companion.classList.add('idle');

  // Tap: pat with particles. Double-tap (or tap when panel closed after pat): open panel
  companion.addEventListener('click', function(e) {
    e.stopPropagation();
    var now = Date.now();

    // Pat reaction with particles
    pat();

    // Toggle panel on tap (single tap pats AND toggles panel)
    if (panel) {
      panelOpen = !panelOpen;
      panel.classList.toggle('open', panelOpen);
      if (panelOpen) refreshStats();
    }
    lastTap = now;
  });

  // Close panel when tapping elsewhere
  document.addEventListener('click', function(e) {
    if (panelOpen && panel && !panel.contains(e.target) && !companion.contains(e.target)) {
      panelOpen = false;
      panel.classList.remove('open');
    }
  });

  function pat() {
    companion.classList.remove('idle', 'patted');
    void companion.offsetWidth; // restart animation
    companion.classList.add('patted');
    setTimeout(function() {
      companion.classList.remove('patted');
      companion.classList.add('idle');
    }, 550);

    // Spawn heart/sparkle particles
    var rect = art.getBoundingClientRect();
    var emojis = ['💧', '✨', '💙'];
    for (var i = 0; i < 3; i++) {
      (function(i) {
        setTimeout(function() {
          var p = document.createElement('div');
          p.className = 'pc-particle';
          p.textContent = emojis[i % emojis.length];
          p.style.left = (rect.left + rect.width / 2 + (Math.random() * 40 - 20)) + 'px';
          p.style.top = (rect.top + 10) + 'px';
          document.body.appendChild(p);
          setTimeout(function() { p.remove(); }, 1000);
        }, i * 120);
      })(i);
    }
  }

  function refreshStats() {
    if (!panel) return;
    var statsEl = panel.querySelector('.pc-stats');
    if (!statsEl) return;
    // Stats are embedded at render time; for fresh data we'd need an endpoint.
    // The panel shows the state from page load, which is fine for quick-care.
  }

  // Quick-care buttons
  if (panel) {
    panel.querySelectorAll('.pc-care-btn').forEach(function(btn) {
      btn.addEventListener('click', function(e) {
        e.stopPropagation();
        var action = btn.getAttribute('data-action');
        doCare(action, btn);
      });
    });
  }

  function doCare(action, btn) {
    var originalText = btn.textContent;
    btn.textContent = '...';
    btn.disabled = true;

    var fd = new FormData();
    if (csrfToken) fd.append('csrf_token', csrfToken);

    fetch('/pet/' + action, {
      method: 'POST',
      body: fd,
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      btn.textContent = originalText;
      btn.disabled = false;
      if (res.ok && res.data.ok) {
        // Happy bounce
        companion.classList.remove('idle');
        void companion.offsetWidth;
        companion.classList.add('patted');
        setTimeout(function() {
          companion.classList.remove('patted');
          companion.classList.add('idle');
        }, 550);
        // Update stats in panel
        var pet = res.data.pet;
        if (pet && panel) {
          var statsEl = panel.querySelector('.pc-stats');
          if (statsEl) {
            statsEl.textContent = pet.name + ' · ' + pet.mood +
              ' · Hunger ' + pet.hunger + ' · Happy ' + pet.happiness;
          }
          // Update the companion art if mood changed the SVG
          if (pet.svg) {
            art.innerHTML = pet.svg;
          }
        }
        showPcToast(res.data.message || 'Done!');
      } else {
        showPcToast((res.data && res.data.error) || 'Try again in a sec.');
      }
    })
    .catch(function() {
      btn.textContent = originalText;
      btn.disabled = false;
      showPcToast('Hmm, try again in a sec.');
    });
  }

  function showPcToast(msg) {
    var t = document.createElement('div');
    t.className = 'tp-toast';
    t.textContent = msg;
    document.body.appendChild(t);
    requestAnimationFrame(function() { t.classList.add('show'); });
    setTimeout(function() {
      t.classList.remove('show');
      setTimeout(function() { t.remove(); }, 500);
    }, 2000);
  }

  // Music bob: dance when audio is playing on episode/music pages
  // The player.js exposes a global audio element or event; we poll gently.
  var danceCheck = setInterval(function() {
    var audio = document.querySelector('audio');
    var isPlaying = false;
    if (audio && !audio.paused && !audio.ended && audio.currentTime > 0) {
      isPlaying = true;
    }
    // Also check for a global player state
    if (window.MusePlayer && window.MusePlayer.isPlaying) {
      try { isPlaying = window.MusePlayer.isPlaying(); } catch (e) {}
    }
    companion.classList.toggle('dancing', isPlaying);
    if (isPlaying) companion.classList.remove('idle');
    else if (!companion.classList.contains('patted')) companion.classList.add('idle');
  }, 1000);

  // Game hooks: expose a global for games to trigger reactions
  window.PetCompanion = {
    cheer: function() {
      // Win reaction: happy bounce + sparkles
      pat();
      showPcToast('🎉 ' + (companion.getAttribute('data-pet-name') || 'Your pet') + ' cheers!');
    },
    pout: function() {
      // Loss reaction: small droop
      art.style.transform = 'scale(.92)';
      setTimeout(function() { art.style.transform = ''; }, 800);
    },
    perk: function(text) {
      showPcToast('✨ Pet perk: ' + text);
    }
  };
})();
