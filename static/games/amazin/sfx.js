/* Amazin' Rush - synthesized sound effects (Web Audio, no audio files).
 * Exposes window.AmazinSfx = { play(name), toggle(), muted }.
 * AudioContext is created lazily on first user gesture (mobile autoplay rules).
 */
(function () {
  'use strict';

  var ctx = null;
  var muted = false;
  try {
    muted = window.localStorage.getItem('ar_muted') === '1';
  } catch (e) {}

  function ensureCtx() {
    if (ctx) {
      if (ctx.state === 'suspended') ctx.resume();
      return ctx;
    }
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    ctx = new AC();
    return ctx;
  }

  // Unlock audio on first touch/click anywhere.
  function unlock() {
    ensureCtx();
    document.removeEventListener('touchstart', unlock);
    document.removeEventListener('click', unlock);
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('touchstart', unlock, { passive: true });
    document.addEventListener('click', unlock);
  }

  function tone(freq, dur, type, vol, when, slideTo) {
    if (muted) return;
    var c = ensureCtx();
    if (!c) return;
    var t = c.currentTime + (when || 0);
    var o = c.createOscillator();
    var g = c.createGain();
    o.type = type || 'sine';
    o.frequency.setValueAtTime(freq, t);
    if (slideTo) o.frequency.exponentialRampToValueAtTime(slideTo, t + dur);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(vol || 0.18, t + 0.012);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g);
    g.connect(c.destination);
    o.start(t);
    o.stop(t + dur + 0.05);
  }

  var SFX = {
    // short UI blip
    blip: function () { tone(660, 0.07, 'square', 0.08); },
    // product tapped
    select: function () { tone(520, 0.06, 'square', 0.09); tone(780, 0.08, 'square', 0.09, 0.05); },
    // added to bag
    pop: function () { tone(300, 0.09, 'sine', 0.22, 0, 640); },
    // ticket tapped
    ticket: function () { tone(440, 0.08, 'triangle', 0.14); tone(587, 0.1, 'triangle', 0.14, 0.07); },
    // new customer arrived
    ding: function () { tone(880, 0.12, 'sine', 0.12); tone(1174, 0.16, 'sine', 0.1, 0.1); },
    // order placed: cash-register cha-ching
    chaChing: function () {
      tone(988, 0.1, 'square', 0.1);
      tone(1319, 0.22, 'square', 0.1, 0.09);
    },
    // perfect order: little fanfare
    fanfare: function () {
      var seq = [523, 659, 784, 1047];
      for (var i = 0; i < seq.length; i++) tone(seq[i], 0.14, 'triangle', 0.16, i * 0.09);
    },
    // streak multiplier up
    streak: function () {
      var seq = [659, 830, 988];
      for (var i = 0; i < seq.length; i++) tone(seq[i], 0.1, 'sawtooth', 0.07, i * 0.06);
    },
    // customer stormed out
    sad: function () { tone(330, 0.18, 'sawtooth', 0.1, 0, 196); tone(196, 0.25, 'sawtooth', 0.1, 0.16, 147); },
    // returns opened: two-tone alert
    alert: function () { tone(740, 0.12, 'square', 0.1); tone(554, 0.14, 'square', 0.1, 0.13); },
    // shift start jingle
    start: function () {
      var seq = [392, 523, 659, 784];
      for (var i = 0; i < seq.length; i++) tone(seq[i], 0.13, 'triangle', 0.15, i * 0.1);
    },
    // shift over jingle
    end: function () {
      var seq = [784, 659, 523, 392];
      for (var i = 0; i < seq.length; i++) tone(seq[i], 0.16, 'triangle', 0.14, i * 0.12);
    },
    // button click
    click: function () { tone(700, 0.05, 'square', 0.06); }
  };

  function play(name) {
    if (SFX[name]) {
      try { SFX[name](); } catch (e) {}
    }
  }

  function toggle() {
    muted = !muted;
    try { window.localStorage.setItem('ar_muted', muted ? '1' : '0'); } catch (e) {}
    return muted;
  }

  if (typeof window !== 'undefined') {
    window.AmazinSfx = { play: play, toggle: toggle, get muted() { return muted; } };
  }
})();
