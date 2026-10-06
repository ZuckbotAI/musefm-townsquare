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
      // Keyboard: the scrub is a slider, arrows seek.
      scrub.addEventListener("keydown", function (ev) {
        var d = audio.duration || 0;
        if (!(d > 0)) return;
        var step = 0;
        if (ev.key === "ArrowLeft") step = -5;
        else if (ev.key === "ArrowRight") step = 5;
        else if (ev.key === "Home") { audio.currentTime = 0; paint(); ev.preventDefault(); return; }
        else if (ev.key === "End") { audio.currentTime = d; paint(); ev.preventDefault(); return; }
        else return;
        audio.currentTime = Math.max(0, Math.min(d, audio.currentTime + step));
        paint();
        ev.preventDefault();
      });
    }

    // Comments: per-track thread panel, fetched on first open.
    var ctoggle = track.querySelector("[data-ctoggle]");
    var cpanel = track.querySelector("[data-comments]");
    var ccount = track.querySelector("[data-ccount]");
    if (ctoggle && cpanel) {
      var cuid = ctoggle.getAttribute("data-ctoggle");
      var cloaded = false;
      var copen = false;
      var csigned = !!document.querySelector('meta[name="csrf-token"]');

      function timeAgo(ts) {
        var s = Math.max(0, Math.floor(Date.now() / 1000) - (ts || 0));
        if (s < 60) return "just now";
        if (s < 3600) { var m = Math.floor(s / 60); return m + "m ago"; }
        if (s < 86400) { var h = Math.floor(s / 3600); return h + "h ago"; }
        var d = Math.floor(s / 86400);
        return d + (d === 1 ? " day ago" : " days ago");
      }

      function showError(msg) {
        var e = cpanel.querySelector(".gc-error");
        if (!e) {
          e = document.createElement("div");
          e.className = "gc-error";
          cpanel.insertBefore(e, cpanel.firstChild);
        }
        e.textContent = msg;
      }

      function commentNode(c, depth) {
        var wrap = document.createElement("div");
        wrap.className = "gc-item";
        var head = document.createElement("div");
        head.className = "gc-head";
        var b = document.createElement("b");
        b.textContent = "@" + c.handle;
        var t = document.createElement("span");
        t.textContent = " " + timeAgo(c.created_at);
        head.appendChild(b); head.appendChild(t);
        var body = document.createElement("div");
        body.className = "gc-body";
        body.innerHTML = c.body_html || "";
        wrap.appendChild(head);
        wrap.appendChild(body);
        if (csigned && depth < 4) {
          var acts = document.createElement("div");
          acts.className = "gc-actions";
          var rb = document.createElement("button");
          rb.className = "gc-reply";
          rb.type = "button";
          rb.textContent = "Reply";
          rb.addEventListener("click", function () {
            toggleReplyForm(wrap, c.id);
          });
          acts.appendChild(rb);
          wrap.appendChild(acts);
        }
        if (c.replies && c.replies.length) {
          var reps = document.createElement("div");
          reps.className = "gc-replies";
          c.replies.forEach(function (r) {
            reps.appendChild(commentNode(r, depth + 1));
          });
          wrap.appendChild(reps);
        }
        return wrap;
      }

      function commentForm(parentId, onDone) {
        var form = document.createElement("form");
        form.className = "gc-form";
        var input = document.createElement("input");
        input.className = "gc-input";
        input.name = "body";
        input.maxLength = 2000;
        input.placeholder = parentId ? "Write a reply…" : "Add a comment…";
        input.setAttribute("autocomplete", "off");
        input.setAttribute("aria-label", parentId ? "Write a reply" : "Add a comment");
        var send = document.createElement("button");
        send.className = "gc-send";
        send.type = "submit";
        send.textContent = "Post";
        form.appendChild(input);
        form.appendChild(send);
        form.addEventListener("submit", function (ev) {
          ev.preventDefault();
          var body = input.value.trim();
          if (!body) return;
          send.disabled = true;
          var meta = document.querySelector('meta[name="csrf-token"]');
          fetch("/api/audio/" + cuid + "/comments", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              parent_id: parentId,
              body: body,
              csrf_token: meta ? meta.getAttribute("content") : ""
            })
          }).then(function (r) { return r.json(); }).then(function (j) {
            send.disabled = false;
            if (j && j.ok) {
              if (typeof j.comment_count === "number" && ccount) {
                ccount.textContent = j.comment_count;
              }
              loadThread();
              if (onDone) onDone();
            } else if (j && j.signin_url) {
              window.location.href = j.signin_url;
            } else {
              showError((j && j.error) || "Could not post, try again.");
            }
          }).catch(function () {
            send.disabled = false;
            showError("Could not post, try again.");
          });
        });
        return form;
      }

      function toggleReplyForm(wrap, parentId) {
        var old = wrap.querySelector(":scope > .gc-form");
        if (old) { old.remove(); return; }
        wrap.appendChild(commentForm(parentId, function () {
          var f = wrap.querySelector(":scope > .gc-form");
          if (f) f.remove();
        }));
        var inp = wrap.querySelector(":scope > .gc-form .gc-input");
        if (inp) inp.focus();
      }

      function renderThread(list) {
        cpanel.innerHTML = "";
        if (csigned) {
          cpanel.appendChild(commentForm(null, null));
        } else {
          var n = document.createElement("div");
          n.className = "gc-signin";
          var a = document.createElement("a");
          a.href = "/login?next=" + encodeURIComponent(window.location.pathname);
          a.textContent = "Sign in to comment";
          n.appendChild(a);
          cpanel.appendChild(n);
        }
        if (!list.length) {
          var e = document.createElement("div");
          e.className = "gc-empty";
          e.textContent = "No comments yet. Say something kind.";
          cpanel.appendChild(e);
          return;
        }
        list.forEach(function (c) {
          cpanel.appendChild(commentNode(c, 0));
        });
      }

      function loadThread() {
        fetch("/api/audio/" + cuid + "/comments?limit=20")
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (j && j.ok) {
              renderThread(j.comments || []);
              if (typeof j.count === "number" && ccount) {
                ccount.textContent = j.count;
              }
            } else {
              showError("Could not load comments.");
            }
          })
          .catch(function () { showError("Could not load comments."); });
      }

      ctoggle.addEventListener("click", function () {
        copen = !copen;
        ctoggle.setAttribute("aria-expanded", copen ? "true" : "false");
        if (copen) {
          cpanel.hidden = false;
          if (!cloaded) { cloaded = true; loadThread(); }
        } else {
          cpanel.hidden = true;
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
