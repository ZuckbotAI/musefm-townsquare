/* Shared auth-page behaviors (2026-10-04, overhaul item 6: login modern standard).
 * - Show/hide toggles on any [data-password-toggle="<input id>"] button.
 * - Caps-lock warning on any [data-caps-watch] password input.
 * - Double-submit guard on any form[data-auth-form]: disables the
 *   [data-auth-submit] button and relabels it while the POST is in flight.
 * No dependencies. Safe to load on any page; no-ops when hooks are absent.
 */
(function () {
  function forEach(sel, fn) {
    Array.prototype.forEach.call(document.querySelectorAll(sel), fn);
  }

  // Show / hide password toggles.
  forEach("[data-password-toggle]", function (toggle) {
    var input = document.getElementById(toggle.getAttribute("data-password-toggle"));
    if (!input) return;
    toggle.setAttribute("aria-controls", input.id);
    toggle.addEventListener("click", function () {
      var showing = input.type === "text";
      input.type = showing ? "password" : "text";
      toggle.textContent = showing ? "Show" : "Hide";
      toggle.setAttribute("aria-pressed", showing ? "false" : "true");
    });
  });

  // Caps-lock warnings.
  forEach("[data-caps-watch]", function (input) {
    var warn = input.closest(".field")
      ? input.closest(".field").querySelector("[data-caps-warn]")
      : null;
    if (!warn) return;
    function onKey(e) {
      var caps = e.getModifierState && e.getModifierState("CapsLock");
      warn.hidden = !caps;
    }
    input.addEventListener("keyup", onKey);
    input.addEventListener("keydown", onKey);
    input.addEventListener("blur", function () { warn.hidden = true; });
  });

  // Double-submit guard.
  forEach("form[data-auth-form]", function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector("[data-auth-submit]");
      if (!btn || btn.disabled) return;
      btn.disabled = true;
      btn.setAttribute("data-original-label", btn.textContent);
      btn.textContent = "Working...";
    });
  });
})();
