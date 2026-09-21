/* Muse FM Listening Room client (2026-09-21).
 * Heartbeat (15s), state poll (5s), premiere sync engine, chat, reactions.
 * Dependency-free.
 */
(function () {
  "use strict";
  var cfg = window.ROOM_CONFIG;
  if (!cfg) return;

  var audio = document.getElementById("room-audio");
  var playBtn = document.getElementById("room-play");
  var progressFill = document.getElementById("room-progress-fill");
  var progressBar = document.getElementById("room-progress");
  var elapsedEl = document.getElementById("room-elapsed");
  var totalEl = document.getElementById("room-total");
  var pill = document.getElementById("room-status-pill");
  var statusText = document.getElementById("room-status-text");
  var listenerCount = document.getElementById("room-listener-count");
  var handlesEl = document.getElementById("room-handles");
  var chatLog = document.getElementById("room-chat-log");
  var chatForm = document.getElementById("room-chat-form");
  var chatInput = document.getElementById("room-chat-input");
  var charCount = document.getElementById("room-char-count");
  var chatHint = document.getElementById("room-chat-hint");
  var syncNote = document.getElementById("room-sync-note");
  var playerCard = document.getElementById("room-player-card");

  var skew = null;          // server_time - local time; first sample kept
  var startedAt = cfg.started_at;
  var endedAt = cfg.ended_at;
  var lastChatId = 0;
  var seenChat = {};
  var seenRxns = {};
  var userWantsPlay = false;
  var seekable = false;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function roomNow() { return Date.now() / 1000 + (skew || 0); }

  function isLive() {
    return !!(startedAt && !endedAt && roomNow() >= startedAt);
  }

  function isReplay() {
    if (endedAt) return true;
    if (startedAt && cfg.duration_sec &&
        roomNow() - startedAt >= cfg.duration_sec) return true;
    return false;
  }

  function setStatus(mode, text) {
    pill.className = "room-pill" + (mode ? " " + mode : "");
    statusText.textContent = text;
  }

  function setSeekable(on) {
    seekable = on;
    progressBar.classList.toggle("seekable", on);
  }

  function syncToRoom() {
    if (!isLive() || !cfg.duration_sec) return;
    var expected = roomNow() - startedAt;
    if (expected < 0 || expected >= cfg.duration_sec) return;
    if (audio.paused || Math.abs(audio.currentTime - expected) > 3) {
      try { audio.currentTime = expected; } catch (e) { /* not ready */ }
      if (userWantsPlay) playAudio();
    }
  }

  function updateStatus() {
    if (isReplay()) {
      setStatus("", "Replay");
      syncNote.textContent = "Premiere complete, replay freely.";
      setSeekable(true);
    } else if (isLive()) {
      setStatus("live", "Live");
      syncNote.textContent =
        "Synced to the room. Everyone hears the same moment.";
      setSeekable(false);
      syncToRoom();
    } else if (startedAt) {
      var remain = Math.max(0, startedAt - roomNow());
      setStatus("soon", "Starts in " + fmt(remain));
      syncNote.textContent =
        "The premiere has not started yet. Stay close.";
      setSeekable(false);
    } else {
      setStatus("soon", "Waiting");
      syncNote.textContent =
        "The premiere has not started yet. Stay close, others are waiting with you.";
      setSeekable(false);
    }
  }

  function playAudio() {
    var p = audio.play();
    if (p && p.catch) p.catch(function () {
      syncNote.textContent = "Tap play to join the room audio.";
    });
  }

  playBtn.addEventListener("click", function () {
    if (audio.paused) { userWantsPlay = true; playAudio(); }
    else { userWantsPlay = false; audio.pause(); }
  });
  audio.addEventListener("play", function () { playBtn.textContent = "⏸"; });
  audio.addEventListener("pause", function () { playBtn.textContent = "▶"; });
  audio.addEventListener("timeupdate", function () {
    var dur = cfg.duration_sec || audio.duration || 0;
    if (dur) {
      progressFill.style.width =
        Math.min(100, (audio.currentTime / dur) * 100) + "%";
    }
    elapsedEl.textContent = fmt(audio.currentTime);
  });
  audio.addEventListener("loadedmetadata", function () {
    if (!cfg.duration_sec && audio.duration) {
      totalEl.textContent = fmt(audio.duration);
    }
  });

  progressBar.addEventListener("click", function (ev) {
    if (!seekable || !cfg.duration_sec) return;
    var r = progressBar.getBoundingClientRect();
    var frac = (ev.clientX - r.left) / r.width;
    audio.currentTime = Math.max(0, Math.min(1, frac)) * cfg.duration_sec;
  });

  function renderHandles(listeners) {
    var shown = listeners.slice(0, 12);
    var html = shown.map(function (l) {
      return '<span class="room-handle-chip">' + esc(l.handle) + "</span>";
    }).join("");
    if (listeners.length > 12) {
      html += '<span class="room-handle-chip">+' +
        (listeners.length - 12) + " more</span>";
    }
    handlesEl.innerHTML = html;
  }

  function timeStr(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: "numeric", minute: "2-digit"
    });
  }

  function appendChat(m) {
    if (seenChat[m.id]) return;
    seenChat[m.id] = true;
    if (m.id > lastChatId) lastChatId = m.id;
    var div = document.createElement("div");
    div.className = "room-msg";
    div.innerHTML = '<span class="who">' + esc(m.handle) + "</span>" +
      '<span class="body">' + esc(m.body) + "</span>" +
      '<span class="when">' + esc(timeStr(m.created_at)) + "</span>";
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  function floatUp(emoji) {
    var s = document.createElement("span");
    s.className = "room-float";
    s.textContent = emoji;
    s.style.left = (10 + Math.random() * 70) + "%";
    s.style.bottom = "120px";
    playerCard.appendChild(s);
    setTimeout(function () { s.remove(); }, 2300);
  }

  function noteReaction(r) {
    var key = r.handle + "|" + r.emoji + "|" + r.created_at;
    if (seenRxns[key]) return;
    seenRxns[key] = true;
    floatUp(r.emoji);
  }

  function applyState(st) {
    if (!st || !st.ok || !st.room) return;
    if (skew === null && typeof st.server_time === "number") {
      skew = st.server_time - Date.now() / 1000;
    }
    startedAt = st.room.started_at;
    endedAt = st.room.ended_at;
    listenerCount.textContent = st.listener_count;
    renderHandles(st.listeners || []);
    (st.chat || []).forEach(appendChat);
    (st.reactions || []).forEach(noteReaction);
    updateStatus();
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
  }

  function pollState() {
    var url = cfg.state_url +
      (lastChatId ? "?since_chat_id=" + lastChatId : "");
    fetch(url, {credentials: "same-origin", cache: "no-store"})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(applyState)
      .catch(function () { /* next poll */ });
  }

  function heartbeat() {
    postJSON(cfg.presence_url, {room_token: cfg.room_token})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(applyState)
      .catch(function () { /* next beat */ });
  }

  chatForm.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var body = chatInput.value.trim();
    if (!body) return;
    chatHint.hidden = true;
    postJSON(cfg.chat_url, {room_token: cfg.room_token, body: body})
      .then(function (r) {
        if (r.ok) {
          chatInput.value = "";
          charCount.textContent = "0";
          pollState();
        } else {
          return r.json().then(function (j) {
            chatHint.textContent =
              (j && j.error) || "Could not send. Try again.";
            chatHint.hidden = false;
          });
        }
      })
      .catch(function () {
        chatHint.textContent = "Could not send. Try again.";
        chatHint.hidden = false;
      });
  });

  chatInput.addEventListener("input", function () {
    charCount.textContent = String(chatInput.value.length);
  });

  document.getElementById("room-reactions").addEventListener(
    "click", function (ev) {
      var btn = ev.target.closest("[data-emoji]");
      if (!btn) return;
      var emoji = btn.getAttribute("data-emoji");
      floatUp(emoji);
      postJSON(cfg.react_url,
               {room_token: cfg.room_token, emoji: emoji})
        .catch(function () { /* ephemeral */ });
    });

  // boot: heartbeat now (registers presence), then steady loops
  heartbeat();
  setInterval(heartbeat, 15000);
  setInterval(pollState, 5000);
  setInterval(updateStatus, 1000);  // countdown ticks + drift re-sync
  updateStatus();
})();
