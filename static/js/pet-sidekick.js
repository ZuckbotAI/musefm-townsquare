/* ============================================================
 * PetSidekick — the pet's game-presence layer (2026-10-05).
 *
 * Drop-in for browser games (first consumer: Amazin' Rush at
 * /games/amazin). Renders the player's OWN pet — same canonical
 * pixel SVG, same mood — in a corner of the game, and gives the
 * game a small, honest API:
 *
 *   var sk = PetSidekick.mount(el, { size: 96 });
 *   sk.cheer();            // win reaction: spin + pixel stars
 *   sk.pout();             // loss reaction: droop + wiggle
 *   sk.sleepy();           // idle / long-session reaction
 *   sk.say("text");        // speech bubble
 *   sk.luckyBounce();      // perk roll -> true/false. base 8%,
 *                          // scaled by the pet's real happiness.
 *                          // On true the pet visibly grants it.
 *   sk.bonusBubble(cb);    // spawns a tappable pixel bonus
 *                          // bubble; cb(points) on tap.
 *   sk.reportScore(n);     // minigame scores feed back into
 *                          // happiness through the REAL
 *                          // /pet/play endpoint (session+CSRF).
 *                          // No mocks, no invented stats.
 *   sk.unmount();
 *
 * Standalone: depends only on PetAnim (already site-wide) and the
 * public /api/pets/of/<handle> status. Zero emoji particles —
 * pixel bursts drawn as crisp rects.
 * ============================================================ */
(function () {
  "use strict";

  var REDUCED = (typeof window.matchMedia === "function") &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function handle() {
    return (document.body && document.body.getAttribute("data-session-handle")) || "";
  }
  function csrf() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  }

  /* tiny pixel burst (standalone; the companion has its own engine) */
  var GLYPHS = {
    star: { w: 9, h: 9, rows: ["....X....", "....X....", ".X..X..X.", "..XXXXX..", "XXXXXXXXX", "..XXXXX..", ".X..X..X.", "....X....", "....X...."], c: "#fbbf24" },
    heart: { w: 7, h: 6, rows: [".XX.XX.", "XXXXXXX", "XXXXXXX", ".XXXXX.", "..XXX..", "...X..."], c: "#fb7185" },
    zzz: { w: 7, h: 7, rows: ["XXXXX..", "....X..", "...X...", "..X....", ".X.....", "XXXXX..", "......."], c: "#818cf8" },
    drop: { w: 5, h: 7, rows: ["..X..", ".XXX.", ".XXX.", "XXXXX", "XXXXX", "XXXXX", ".XXX."], c: "#38bdf8" }
  };
  function burst(host, kind, n) {
    if (REDUCED || !host) return;
    var g = GLYPHS[kind] || GLYPHS.star;
    n = n || 6;
    var cs = window.getComputedStyle(host);
    if (cs.position === "static") host.style.position = "relative";
    for (var i = 0; i < n; i++) {
      (function () {
        var s = document.createElement("span");
        s.setAttribute("aria-hidden", "true");
        var px = 3, w = g.w * px, h = g.h * px;
        var inner = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' +
          g.w + " " + g.h + '"><g shape-rendering="crispEdges" fill="' + g.c + '">';
        for (var r = 0; r < g.h; r++)
          for (var c = 0; c < g.w; c++)
            if (g.rows[r].charAt(c) === "X")
              inner += '<rect x="' + c + '" y="' + r + '" width="1" height="1"/>';
        s.innerHTML = inner + "</g></svg>";
        s.style.cssText = "position:absolute;left:50%;top:30%;z-index:5;" +
          "pointer-events:none;transform:translate(-50%,-50%);";
        host.appendChild(s);
        var dx = (Math.random() - 0.5) * 110, dy = -(40 + Math.random() * 60);
        var a = s.animate([
          { transform: "translate(-50%,-50%)", opacity: 1 },
          { transform: "translate(calc(-50% + " + dx + "px)," + dy + "px)", opacity: 0 }
        ], { duration: 800 + Math.random() * 300, easing: "cubic-bezier(.2,.7,.3,1)" });
        a.onfinish = function () { s.remove(); };
      })();
    }
  }

  function mount(el, opts) {
    opts = opts || {};
    var size = opts.size || 96;
    var h = handle();
    var api = {
      el: el, svg: null, status: null, dead: false,
      cheer: function () {
        if (!this.svg || !window.PetAnim) return;
        try { window.PetAnim.celebrate(this.svg); } catch (e) {}
        burst(el, "star", 8);
      },
      pout: function () {
        if (!this.svg || !window.PetAnim) return;
        var s = this.svg;
        try { window.PetAnim.wiggle(s); } catch (e) {}
        var wrap = el.querySelector(".sk-stage");
        if (wrap && !REDUCED) {
          wrap.animate([
            { transform: "translateY(0) scale(1,1)" },
            { transform: "translateY(4px) scale(1.04,.92)" },
            { transform: "translateY(0) scale(1,1)" }
          ], { duration: 900, easing: "ease-in-out" });
        }
        burst(el, "drop", 4);
      },
      sleepy: function () {
        if (!this.svg || !window.PetAnim) return;
        try { window.PetAnim.rest(this.svg); } catch (e) {}
        burst(el, "zzz", 5);
      },
      say: function (text, ms) {
        var b = el.querySelector(".sk-bubble");
        if (!b) return;
        b.textContent = text;
        b.classList.add("show");
        setTimeout(function () { b.classList.remove("show"); }, ms || 3500);
      },
      /* Good care earns small in-game luck. The roll is honest:
       * base 8% + happiness/500, capped at 35%. */
      luckyBounce: function () {
        var hap = (this.status && this.status.happiness) || 50;
        var chance = Math.min(0.35, 0.08 + hap / 500);
        var win = Math.random() < chance;
        if (win) {
          this.cheer();
          this.say("lucky bounce! my happiness did that.");
        }
        return win;
      },
      bonusBubble: function (cb, points) {
        points = points || 25;
        var b = document.createElement("button");
        b.type = "button";
        b.className = "sk-bonus";
        b.setAttribute("aria-label", "Bonus bubble, tap for " + points + " points");
        b.innerHTML = '<svg width="34" height="34" viewBox="0 0 11 11">' +
          '<g shape-rendering="crispEdges"><rect x="3" y="1" width="5" height="9" fill="#38bdf8"/>' +
          '<rect x="1" y="3" width="9" height="5" fill="#38bdf8"/>' +
          '<rect x="3" y="3" width="2" height="2" fill="#e0f2fe"/></g></svg>' +
          '<span>+' + points + "</span>";
        var self = this;
        b.addEventListener("click", function () {
          burst(b, "star", 6);
          try { window.PetAnim.pat(self.svg, false); } catch (e) {}
          b.remove();
          if (cb) cb(points);
        });
        el.appendChild(b);
        // drift up and pop if ignored
        b.animate([
          { transform: "translateY(0)", opacity: 1 },
          { transform: "translateY(-46px)", opacity: 0 }
        ], { duration: 6000, easing: "ease-out" }).onfinish = function () { b.remove(); };
      },
      /* Minigame scores feed back into real happiness: a good score
       * fires the genuine /pet/play care action (same cooldowns and
       * server truth as the quick-care panel). */
      reportScore: function (score, threshold) {
        var self = this;
        threshold = threshold == null ? 1 : threshold;
        if (score >= (threshold * 2)) this.cheer();
        else if (score >= threshold) { try { window.PetAnim.wiggle(this.svg); } catch (e) {} }
        else this.pout();
        if (score < threshold || !h) return Promise.resolve({ ok: false, reason: "below threshold" });
        var body = new URLSearchParams();
        body.set("csrf_token", csrf());
        return fetch("/pet/play", {
          method: "POST", credentials: "same-origin",
          headers: { "Accept": "application/json" }, body: body
        }).then(function (r) { return r.json(); }).then(function (d) {
          if (d && d.ok && d.pet) {
            self.status.happiness = d.pet.happiness;
            self.say("that game made me happier!");
            burst(el, "heart", 5);
          }
          return d;
        }).catch(function () { return { ok: false, reason: "offline" }; });
      },
      unmount: function () { this.dead = true; el.innerHTML = ""; }
    };

    el.classList.add("sk-root");
    el.innerHTML =
      '<div class="sk-bubble" role="status"></div>' +
      '<div class="sk-stage" style="width:' + size + "px;height:" + size + 'px"></div>' +
      '<div class="sk-name"></div>';
    var css = document.createElement("style");
    css.textContent =
      ".sk-root{position:relative;font-family:inherit}" +
      ".sk-stage{border:3px solid #0b1e3a;background:#fffdf6;box-shadow:4px 4px 0 rgba(11,30,58,.9);" +
      "display:flex;align-items:flex-end;justify-content:center}" +
      ".sk-stage svg{width:88%;height:88%;image-rendering:pixelated}" +
      ".sk-bubble{position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%) translateY(6px);" +
      "width:180px;background:#fffdf6;border:3px solid #0b1e3a;box-shadow:4px 4px 0 rgba(11,30,58,.9);" +
      "padding:8px 10px;font-size:13px;font-weight:700;color:#0b1e3a;opacity:0;transition:opacity .18s,transform .18s;" +
      "pointer-events:none;z-index:3}" +
      ".sk-bubble.show{opacity:1;transform:translateX(-50%) translateY(0)}" +
      ".sk-name{text-align:center;font-size:11px;font-weight:800;color:#0b1e3a;margin-top:4px}" +
      ".sk-bonus{position:absolute;left:50%;top:-6px;transform:translateX(-50%);background:none;border:0;" +
      "cursor:pointer;display:flex;flex-direction:column;align-items:center;z-index:4;min-width:48px;min-height:48px}" +
      ".sk-bonus span{font-size:11px;font-weight:800;color:#0b1e3a;background:#fbbf24;border:2px solid #0b1e3a;" +
      "padding:1px 5px;margin-top:2px}";
    document.head.appendChild(css);

    if (!h) { el.querySelector(".sk-name").textContent = "no pet yet"; return api; }
    fetch("/api/pets/of/" + encodeURIComponent(h), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (api.dead || !d || !d.ok || !d.adopted || !d.svg_large) return;
        api.status = d;
        var stage = el.querySelector(".sk-stage");
        stage.innerHTML = d.svg_large;
        api.svg = stage.querySelector("svg");
        if (api.svg) { api.svg.removeAttribute("width"); api.svg.removeAttribute("height"); }
        el.querySelector(".sk-name").textContent = d.name + " · " + (d.mood || "");
        if (window.PetAnim && window.PetAnim.init) {
          try { window.PetAnim.init(); } catch (e) {}
          try { window.PetAnim.wiggle(api.svg); } catch (e) {}
        }
      })
      .catch(function () { /* stay empty offline */ });
    return api;
  }

  window.PetSidekick = { mount: mount };
})();
