/* eAFYA Assistant — custom JS.
 * Loaded by Chainlit via .chainlit/config.toml > [UI] custom_js.
 *
 * Callouts: Caution / Note lines arrive as Markdown blockquotes; we tag them
 * `emr-caution` / `emr-note` by their text so style.css can colour them.
 *
 * Buttons: Chainlit renders the cross-reference action buttons ("📄 Open: …")
 * with internal, version-dependent class names that are hard to target from
 * CSS. Instead of guessing selectors, we find those buttons by their visible
 * text (which we control via the action label) and tag them with our own
 * class, `emr-cta`. public/style.css then styles `.emr-cta` as a green
 * call-to-action. The chat is a single-page app that re-renders as messages
 * stream in, so we re-scan on DOM mutations (debounced).
 */
(function () {
  "use strict";

  // Matches our action labels: "📄 Open: Employee Portal", "Open: Laboratory", etc.
  var OPEN_RE = /^\s*(?:📄\s*)?Open:\s+/i;

  // Caution / Note lines in answers are Markdown blockquotes (app/rag/callouts.py).
  var CAUTION_RE = /^\s*(?:⚠️|⚠)?\s*Caution:/i;
  var NOTE_RE = /^\s*Note:/i;

  function tagCallouts() {
    var quotes = document.querySelectorAll("blockquote");
    for (var i = 0; i < quotes.length; i++) {
      var q = quotes[i];
      // Check the class itself, not a marker attribute: React re-renders the
      // message when it finishes streaming and resets className, and the
      // box must be tagged again.
      if (q.classList.contains("emr-callout")) continue;
      var txt = (q.textContent || "").replace(/\s+/g, " ").trim();
      var cls = CAUTION_RE.test(txt) ? "emr-caution" : NOTE_RE.test(txt) ? "emr-note" : "";
      if (!cls) continue;
      q.classList.add("emr-callout", cls);
    }
  }

  function tag() {
    tagCallouts();
    var els = document.querySelectorAll('button, a, [role="button"]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.dataset && el.dataset.emrCta) continue; // already tagged
      var txt = (el.textContent || "").replace(/\s+/g, " ").trim();
      if (OPEN_RE.test(txt)) {
        if (el.dataset) el.dataset.emrCta = "1";
        el.classList.add("emr-cta");
      }
    }
  }

  // Debounce: streaming answers fire many DOM mutations; coalesce the rescans.
  var pending = false;
  function schedule() {
    if (pending) return;
    pending = true;
    setTimeout(function () {
      pending = false;
      tag();
    }, 250);
  }

  function start() {
    tag();
    try {
      new MutationObserver(schedule).observe(document.body, {
        childList: true,
        subtree: true,
      });
    } catch (e) {
      // If MutationObserver is unavailable, fall back to a gentle poll.
      setInterval(tag, 1500);
    }
  }

  if (document.body) start();
  else window.addEventListener("DOMContentLoaded", start);
})();
