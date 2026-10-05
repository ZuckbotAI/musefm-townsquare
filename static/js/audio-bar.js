/* Member audio bar (Jams/Jabbers, 2026-10-05).
 * One <audio> per card, only one plays at a time. No dependencies.
 */
(function () {
  "use strict";

  function fmt(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    var m = Math.floor(sec / 60);
    var s = Math.floor(sec % 60);
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function pauseOthers(except) {
    document.querySelectorAll(".glass-track[data-playing]").forEach(function (el) {
      if (el === except) return;
      var a = el.querySelector("[data-audio]");
      if (a) a.pause();
    });
  }

  document.querySelectorAll(".glass-track").forEach(function (track) {
    var audio = track.querySelector("[data-audio]");
    var btn = track.querySelector("[data-play]");
    var scrub = track.querySelector("[data-scrub]");
    var fill = track.querySelector("[data-fill]");
    var elapsed = track.querySelector("[data-elapsed]");
    var total = track.querySelector("[data-total]");
    if (!audio || !btn) return;

    function paint() {
      var d = audio.duration || 0;
      var c = audio.currentTime || 0;
      if (fill) fill.style.width = (d > 0 ? (c / d) * 100 : 0) + "%";
      if (elapsed) elapsed.textContent = fmt(c);
      if (total && d > 0) total.textContent = fmt(d);
    }

    btn.addEventListener("click", function () {
      if (audio.paused) {
        pauseOthers(track);
        audio.play();
      } else {
        audio.pause();
      }
    });

    audio.addEventListener("play", function () {
      track.setAttribute("data-playing", "1");
      btn.innerHTML = "&#10074;&#10074;";
      btn.setAttribute("aria-label", "Pause");
    });
    function onStop() {
      track.removeAttribute("data-playing");
      btn.innerHTML = "&#9654;";
      btn.setAttribute("aria-label", "Play");
    }
    audio.addEventListener("pause", onStop);
    audio.addEventListener("ended", function () {
      audio.currentTime = 0;
      onStop();
      paint();
    });
    audio.addEventListener("timeupdate", paint);
    audio.addEventListener("loadedmetadata", paint);

    if (scrub) {
      scrub.addEventListener("click", function (ev) {
        var r = scrub.getBoundingClientRect();
        var frac = (ev.clientX - r.left) / r.width;
        frac = Math.max(0, Math.min(1, frac));
        var d = audio.duration || 0;
        if (d > 0) {
          audio.currentTime = frac * d;
          paint();
        }
      });
    }

    // Flag link: one tap sends target_type=upload to /flag (JSON).
    var flag = track.querySelector("[data-flag]");
    if (flag) {
      flag.addEventListener("click", function () {
        if (flag.classList.contains("done")) return;
        var meta = document.querySelector('meta[name="csrf-token"]');
        fetch("/flag", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            target_type: "upload",
            target_id: parseInt(flag.getAttribute("data-flag"), 10),
            reason: "other",
            csrf_token: meta ? meta.getAttribute("content") : ""
          })
        }).then(function (r) { return r.json(); }).then(function (j) {
          if (j && (j.ok || j.flagged)) {
            flag.classList.add("done");
            flag.textContent = "flagged, thanks";
          }
        }).catch(function () { /* leave the link in place */ });
      });
    }

    paint();
  });
})();
