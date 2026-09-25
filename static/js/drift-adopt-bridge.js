/* Townsquare bridge for the Maker's Row 3D village.
 *
 * 1. In-town adoption -> real backend: wraps the Pet Shop module's adopt()
 *    (hooked via string replacement at serve time) so it POSTs
 *    /api/drift/adopt first. Only on success does the local adopt proceed.
 * 2. 3D follower: after a successful adoption, attaches a matching 3D pet
 *    to follow the player robot (follow-lag + hop-bob live in the village
 *    tick already).
 *
 * Loaded after the village bundle. All hooks are guarded — if the village
 * APIs aren't there, adoption falls back to the module's local flow.
 */
(function () {
  'use strict';

  /* Village species id -> backend species key. PET-CUTOVER 2026-09-24:
     backend species keys are now the roster keys themselves (unified),
     so this map is the identity — kept explicit so any future rename
     has exactly one place to change. */
  var SPECIES_MAP = {
    cinder: 'cinder',  /* Puppy -> Bubble Retriever */
    briar: 'briar',    /* Cat -> Seal Pup */
    brine: 'brine',    /* Water -> Droplet Sprite */
    plume: 'plume',    /* Bird -> Koi Wisp */
    crag: 'crag',      /* Rock -> Pearl Crab (locked display design) */
    rust: 'rust'       /* Fox -> Wave Pup */
  };

  function toast(msg) {
    try {
      var el = document.createElement('div');
      el.textContent = msg;
      el.style.cssText = 'position:fixed;left:50%;bottom:76px;transform:translateX(-50%);' +
        'background:#1c1917;color:#fefce8;padding:10px 18px;border-radius:999px;' +
        'font:600 14px/1.4 system-ui,sans-serif;z-index:99999;box-shadow:0 6px 24px rgba(0,0,0,.35);' +
        'max-width:min(92vw,480px);text-align:center;';
      document.body.appendChild(el);
      setTimeout(function () { try { el.remove(); } catch (e) {} }, 4200);
    } catch (e) {}
  }

  /* Attach a 3D follower for the adopted pet. Picks an unowned 3D pet of
     the same village species (falls back to any unowned pet). */
  function attachFollower(pet) {
    try {
      var player = window.__playerDebug;
      var pets = window.__petDebug;
      if (!player || !player.g || !Array.isArray(pets)) return;
      var target = null, i;
      for (i = 0; i < pets.length; i++) {
        if (!pets[i].ownerId && pets[i].species === pet.species && pets[i].g) {
          target = pets[i]; break;
        }
      }
      if (!target) {
        for (i = 0; i < pets.length; i++) {
          if (!pets[i].ownerId && pets[i].g) { target = pets[i]; break; }
        }
      }
      if (!target) return;
      try { target.name = pet.name; } catch (eN) {}
      try { target.ownerId = player.uid || 'me'; } catch (eO) {}
      target.mode = 'follow';
      target.host = player;
      target.off = { x: 0.9, z: 0.6 };
    } catch (e) {}
  }

  /* Server-first adoption. proceed() runs the module's local adopt flow. */
  window.__driftAdoptBridge = function (pet, proceed) {
    var species = SPECIES_MAP[pet.species] || 'bloop';
    var name = String(pet.name || 'Pal').slice(0, 24);
    fetch('/api/csrf-token', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok || !d.csrf_token) {
          toast('Log in to adopt a pet for real — playing as guest.');
          return null;
        }
        return fetch('/api/drift/adopt', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            species: species, name: name,
            confirm: true, csrf_token: d.csrf_token
          })
        });
      })
      .then(function (r) {
        if (!r) return null;
        return r.json().then(function (j) { return { status: r.status, body: j }; });
      })
      .then(function (res) {
        if (!res) return;
        if (res.body && res.body.ok) {
          proceed();
          attachFollower(pet);
        } else {
          toast('Adoption hit a snag: ' +
            ((res.body && res.body.error) || 'try again'));
        }
      })
      .catch(function () {
        toast('Adoption hit a snag — check your connection.');
      });
  };
})();
