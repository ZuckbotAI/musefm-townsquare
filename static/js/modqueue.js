/* modqueue.js — bulk actions, AJAX single actions, and flag filters for
 * the mod queue (/mod/uploads and /mod/flags). No dependencies; uses the
 * global toast() from app.js when present. Non-JS fallback: the underlying
 * forms still POST and redirect as before.
 */
(function () {
  "use strict";

  var AJAX_HEADER = "XMLHttpRequest";

  function say(msg) {
    if (typeof window.toast === "function") window.toast(msg);
  }

  function postForm(url, fields) {
    var fd = new FormData();
    Object.keys(fields).forEach(function (k) { fd.append(k, fields[k]); });
    return fetch(url, {
      method: "POST",
      body: fd,
      headers: { "X-Requested-With": AJAX_HEADER }
    }).then(function (resp) {
      if (!resp.ok) throw new Error("http " + resp.status);
      return resp.json();
    });
  }

  function bumpCount(selector, delta) {
    document.querySelectorAll(selector).forEach(function (el) {
      var n = parseInt(el.textContent, 10);
      if (!isNaN(n)) el.textContent = String(Math.max(0, n + delta));
    });
  }

  function visibleChecks() {
    return Array.prototype.filter.call(
      document.querySelectorAll(".mod-item-check"),
      function (cb) { return cb.offsetParent !== null; }
    );
  }

  function checkedBoxes() {
    return visibleChecks().filter(function (cb) { return cb.checked; });
  }

  function updateSelCount() {
    var n = checkedBoxes().length;
    var label = document.getElementById("mod-sel-count");
    if (label) label.textContent = n + " selected";
    var all = document.getElementById("mod-select-all");
    if (all) {
      var vis = visibleChecks();
      all.checked = vis.length > 0 && vis.every(function (cb) { return cb.checked; });
    }
  }

  function removeCard(card, afterRemove) {
    if (!card || card.classList.contains("mod-leaving")) return;
    card.classList.add("mod-leaving");
    window.setTimeout(function () {
      if (card.parentNode) card.parentNode.removeChild(card);
      updateSelCount();
      if (typeof afterRemove === "function") afterRemove();
    }, 260);
  }

  function cardForCheck(cb) {
    var card = cb.closest(".mod-card");
    return card;
  }

  /* ---- single approve/reject/dismiss/action forms: AJAX, no reload ---- */
  function wireSingleForms() {
    document.querySelectorAll("form.mod-single-form").forEach(function (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var btn = form.querySelector("button[type=submit]");
        if (btn) btn.disabled = true;
        var card = form.closest(".mod-card");
        var isFlag = card && card.classList.contains("mod-flag");
        var fd = new FormData(form);
        fetch(form.action, {
          method: "POST",
          body: fd,
          headers: { "X-Requested-With": AJAX_HEADER }
        }).then(function (resp) {
          if (!resp.ok) throw new Error("http " + resp.status);
          return resp.json();
        }).then(function (data) {
          if (data && data.ok) {
            if (isFlag) {
              removeCard(card, function () { bumpOpenCount(-1); });
            } else {
              var kind = card ? card.getAttribute("data-kind") : null;
              removeCard(card, function () {
                if (kind) bumpCount('.mod-count[data-count-kind="' + kind + '"]', -1);
                maybeShowCleared();
              });
            }
          } else {
            throw new Error("not ok");
          }
        }).catch(function () {
          if (btn) btn.disabled = false;
          say("Something went wrong — try again.");
        });
      });
    });
  }

  /* ---- uploads page: bulk approve/reject ---- */
  function wireUploadBulk() {
    var approveBtn = document.getElementById("mod-bulk-approve");
    var rejectBtn = document.getElementById("mod-bulk-reject");
    if (!approveBtn && !rejectBtn) return;

    function run(action) {
      var boxes = checkedBoxes();
      if (!boxes.length) { say("Select something first."); return; }
      var byKind = {};
      boxes.forEach(function (cb) {
        var k = cb.getAttribute("data-kind") || "video";
        (byKind[k] = byKind[k] || []).push(cb.getAttribute("data-id"));
      });
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
      var kinds = Object.keys(byKind);
      var chain = Promise.resolve();
      kinds.forEach(function (kind) {
        chain = chain.then(function () {
          return postForm("/mod/uploads/bulk", {
            kind: kind, ids: byKind[kind].join(","), action: action
          }).then(function (data) {
            if (data && data.ok) {
              (data.processed || []).forEach(function (id) {
                var card = document.querySelector(
                  '.mod-card[data-kind="' + kind + '"][data-id="' + id + '"]');
                removeCard(card, function () {
                  bumpCount('.mod-count[data-count-kind="' + kind + '"]', -1);
                });
              });
            }
          });
        });
      });
      chain.then(function () {
        maybeShowCleared();
        say(kinds.length ? "Done." : "Nothing processed.");
      }).catch(function () {
        say("Something went wrong — try again.");
      }).then(function () {
        approveBtn.disabled = false;
        rejectBtn.disabled = false;
        var all = document.getElementById("mod-select-all");
        if (all) all.checked = false;
        updateSelCount();
      });
    }

    approveBtn.addEventListener("click", function () { run("approve"); });
    rejectBtn.addEventListener("click", function () { run("reject"); });
  }

  /* When every card on the uploads page is gone, offer the next page. */
  function maybeShowCleared() {
    window.setTimeout(function () {
      if (document.querySelectorAll(".mod-card").length !== 0) return;
      if (document.getElementById("mod-all-cleared")) return;
      var next = null;
      document.querySelectorAll(".mod-pager a").forEach(function (a) {
        if (/next/i.test(a.textContent)) next = a;
      });
      var div = document.createElement("div");
      div.className = "card mod-cleared";
      div.id = "mod-all-cleared";
      div.innerHTML = next
        ? '<p class="hint">Page cleared. 🎉 <a class="btn btn-sm" href="' +
          next.getAttribute("href") + '">Next page →</a></p>'
        : '<p class="hint">All caught up. 🎉</p>';
      var bar = document.getElementById("mod-bulkbar");
      if (bar && bar.parentNode) bar.parentNode.insertBefore(div, bar.nextSibling);
    }, 400);
  }

  /* ---- flags page: bulk dismiss/action + filter chips ---- */
  function bumpOpenCount(delta) {
    bumpCount("#mod-open-count", delta);
    var word = document.getElementById("mod-open-word");
    var countEl = document.getElementById("mod-open-count");
    if (word && countEl) {
      var n = parseInt(countEl.textContent, 10);
      word.textContent = (n === 1) ? "flag" : "flags";
    }
  }

  function wireFlagBulk() {
    var dismissBtn = document.getElementById("mod-bulk-dismiss");
    var actionBtn = document.getElementById("mod-bulk-action");
    if (!dismissBtn && !actionBtn) return;

    function run(action) {
      var boxes = checkedBoxes();
      if (!boxes.length) { say("Select something first."); return; }
      var ids = boxes.map(function (cb) { return cb.getAttribute("data-id"); });
      dismissBtn.disabled = true;
      actionBtn.disabled = true;
      postForm("/mod/flags/bulk-resolve", { ids: ids.join(","), action: action })
        .then(function (data) {
          if (data && data.ok) {
            (data.processed || []).forEach(function (id) {
              var card = document.querySelector('.mod-flag[data-id="' + id + '"]');
              removeCard(card, function () { bumpOpenCount(-1); });
            });
            say("Done.");
          } else {
            throw new Error("not ok");
          }
        })
        .catch(function () { say("Something went wrong — try again."); })
        .then(function () {
          dismissBtn.disabled = false;
          actionBtn.disabled = false;
          var all = document.getElementById("mod-select-all");
          if (all) all.checked = false;
          updateSelCount();
        });
    }

    dismissBtn.addEventListener("click", function () { run("dismissed"); });
    actionBtn.addEventListener("click", function () { run("actioned"); });
  }

  function wireFlagFilters() {
    var chips = document.querySelectorAll(".mod-filter-group .chip");
    if (!chips.length) return;
    var filters = { reason: "all", target: "all" };

    function apply() {
      var visible = 0;
      document.querySelectorAll(".mod-flag").forEach(function (card) {
        var okReason = filters.reason === "all" ||
          card.getAttribute("data-reason") === filters.reason;
        var okTarget = filters.target === "all" ||
          card.getAttribute("data-target-type") === filters.target;
        var ok = okReason && okTarget;
        card.style.display = ok ? "" : "none";
        if (ok) {
          visible++;
        } else {
          var cb = card.querySelector(".mod-item-check");
          if (cb) cb.checked = false;
        }
      });
      var note = document.getElementById("mod-no-match");
      if (note) note.hidden = visible !== 0;
      updateSelCount();
    }

    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var key = chip.getAttribute("data-filter");
        filters[key] = chip.getAttribute("data-value");
        var group = chip.closest(".mod-filter-group");
        group.querySelectorAll(".chip").forEach(function (c) {
          c.classList.toggle("is-active", c === chip);
        });
        apply();
      });
    });
  }

  /* ---- select-all + checkbox bookkeeping (both pages) ---- */
  function wireSelection() {
    var all = document.getElementById("mod-select-all");
    if (all) {
      all.addEventListener("change", function () {
        visibleChecks().forEach(function (cb) { cb.checked = all.checked; });
        updateSelCount();
      });
    }
    document.querySelectorAll(".mod-item-check").forEach(function (cb) {
      cb.addEventListener("change", updateSelCount);
    });
    updateSelCount();
  }

  function init() {
    if (!document.getElementById("mod-bulkbar")) return;
    wireSelection();
    wireSingleForms();
    wireUploadBulk();
    wireFlagBulk();
    wireFlagFilters();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
