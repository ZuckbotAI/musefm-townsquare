/* Home Jams & Jabs rail mini players (2026-10-05 rev 2, Anthony).
 * Each rail row plays its audio inline. One <audio> per row, only one
 * plays at a time. No dependencies.
 */
(function () {
  "use strict";

  function pauseOthers(except) {
    document.querySelectorAll("[data-rail-track][data-playing]").forEach(function (el) {
      if (el === except) return;
      var a = el.querySelector("[data-rail-audio]");
      if (a) a.pause();
    });
    document.querySelectorAll(".glass-track[data-playing]").forEach(function (el) {
      var a = el.querySelector("[data-audio]");
      if (a) a.pause();
    });
  }

  document.querySelectorAll("[data-rail-track]").forEach(function (row) {
    var audio = row.querySelector("[data-rail-audio]");
    var btn = row.querySelector("[data-rail-play]");
    if (!audio || !btn) return;

    btn.addEventListener("click", function () {
      if (audio.paused) {
        pauseOthers(row);
        audio.play();
      } else {
        audio.pause();
      }
    });

    audio.addEventListener("play", function () {
      row.setAttribute("data-playing", "1");
      btn.innerHTML = "&#10074;&#10074;";
      btn.setAttribute("aria-label", "Pause");
    });
    function onStop() {
      row.removeAttribute("data-playing");
      btn.innerHTML = "&#9654;";
      btn.setAttribute("aria-label", "Play");
    }
    audio.addEventListener("pause", onStop);
    audio.addEventListener("ended", function () {
      audio.currentTime = 0;
      onStop();
    });
  });
})();
