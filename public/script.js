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

  // Matches our action labels: "📄 Open: Employee Portal", "Open: Laboratory",
  // and the suggested questions under the greeting, "💬 Register a new patient"
  // (app/ui/starters.py).
  var OPEN_RE = /^\s*(?:(?:📄\s*)?Open:\s+|💬\s)/i;

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

  // The opening messages (app/ui/welcome.py): the trial note and the greeting
  // each get their own light background. Found by their opening words.
  var TRIAL_RE = /^\s*🧪\s*Trial version/;
  var WELCOME_RE = /^\s*👋\s*Hi! I'm the eAFYA Assistant/;

  function tagWelcome() {
    var boxes = document.querySelectorAll(".message-content");
    for (var i = 0; i < boxes.length; i++) {
      var b = boxes[i];
      // By class, not a marker: React resets className when it re-renders.
      if (b.classList.contains("emr-trial") || b.classList.contains("emr-welcome")) continue;
      var txt = (b.textContent || "").trim();
      if (TRIAL_RE.test(txt)) b.classList.add("emr-trial");
      else if (WELCOME_RE.test(txt)) b.classList.add("emr-welcome");
    }
  }

  function tag() {
    tagCallouts();
    tagWelcome();
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

  // --- Account links in the user menu (the circle at the top right) ---------
  // Chainlit has no setting for adding items to this menu, so they are added
  // here when it opens, just above Logout. Who is logged in comes from
  // /account/me, so "Manage accounts" follows a role change. This only decides
  // which links appear: /admin and /account check the login and role
  // themselves. They open in a new tab because leaving the chat page would
  // lose the conversation.
  //
  // At a facility installation without logins, a guest sees only "Super user
  // login" (in the same tab, since logging in starts a new chat anyway) and no
  // Logout, because a guest has nothing to log out of.
  var SVG_OPEN =
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="ml-auto">';
  var ACCOUNT_ITEMS = [
    {
      key: "login",
      href: "/account/login",
      label: "Super user login",
      guestOnly: true,
      sameTab: true,
      icon:
        SVG_OPEN +
        '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/>' +
        '<line x1="15" x2="3" y1="12" y2="12"/></svg>',
    },
    {
      key: "password",
      href: "/account",
      label: "Change your password",
      icon:
        SVG_OPEN +
        '<path d="M2 18v3c0 .6.4 1 1 1h4v-3h3v-3h2l1.4-1.4a6.5 6.5 0 1 0-4-4Z"/>' +
        '<circle cx="16.5" cy="7.5" r=".5" fill="currentColor"/></svg>',
    },
    {
      key: "admin",
      href: "/admin",
      label: "Manage accounts",
      superUserOnly: true,
      icon:
        SVG_OPEN +
        '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>' +
        '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    },
  ];
  var accountMe = null; // {username, role, guest?} from /account/me
  var meRequested = false;
  var meLoading = false;

  function loadAccountRole() {
    if (meLoading) return;
    meLoading = true;
    meRequested = true;
    fetch("/account/me", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (me) {
        accountMe = me;
      })
      .catch(function () {
        accountMe = null;
      })
      .then(function () {
        meLoading = false;
        addAccountItems();
      });
  }

  function showsItem(item) {
    if (!accountMe) return false;
    if (accountMe.guest) return !!item.guestOnly;
    if (item.guestOnly) return false;
    return !item.superUserOnly || accountMe.role === "super_user";
  }

  function closeUserMenu() {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  }

  function accountMenuItem(item, template) {
    var link = document.createElement("a");
    link.href = item.href;
    if (!item.sameTab) {
      link.target = "_blank";
      link.rel = "noopener";
    }
    link.setAttribute("role", "menuitem");
    link.setAttribute("tabindex", "-1");
    // Same look as Logout. Its highlight follows focus.
    link.className = template.className + " emr-menu-item";
    link.style.cursor = "pointer";
    link.dataset.emrMenu = item.key;
    link.innerHTML = "<span>" + item.label + "</span>" + item.icon;
    link.addEventListener("mouseenter", function () {
      link.focus();
    });
    link.addEventListener("mouseleave", function () {
      link.blur();
    });
    link.addEventListener("click", function () {
      setTimeout(closeUserMenu, 0);
    });
    return link;
  }

  function addAccountItems() {
    var trigger = document.getElementById("user-nav-button");
    if (!trigger || trigger.getAttribute("data-state") !== "open") return;
    var menu = document.querySelector('[role="menu"][data-state="open"]');
    if (!menu) return;
    var own = menu.querySelectorAll('[role="menuitem"]:not(.emr-menu-item)');
    if (!own.length) return;
    var logout = own[own.length - 1];
    ACCOUNT_ITEMS.forEach(function (item) {
      var show = showsItem(item);
      var existing = menu.querySelector('[data-emr-menu="' + item.key + '"]');
      if (show && !existing) logout.parentNode.insertBefore(accountMenuItem(item, logout), logout);
      if (!show && existing) existing.parentNode.removeChild(existing);
    });
    var hideLogout = !!(accountMe && accountMe.guest);
    if ((logout.style.display === "none") !== hideLogout) logout.style.display = hideLogout ? "none" : "";
  }

  // Know who is logged in before the menu is first opened; forget it after
  // logging out.
  function trackAccount() {
    if (!document.getElementById("user-nav-button")) {
      accountMe = null;
      meRequested = false;
    } else if (!meRequested) {
      loadAccountRole();
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
      trackAccount();
    }, 250);
  }

  function start() {
    tag();
    trackAccount();
    // Re-read the role each time the menu is opened, so a change shows at once.
    document.addEventListener(
      "pointerdown",
      function (e) {
        if (e.target && e.target.closest && e.target.closest("#user-nav-button")) loadAccountRole();
      },
      true
    );
    try {
      new MutationObserver(function () {
        addAccountItems(); // not debounced: the links must not pop in late
        schedule();
      }).observe(document.body, {
        childList: true,
        subtree: true,
      });
    } catch (e) {
      // If MutationObserver is unavailable, fall back to a gentle poll.
      setInterval(function () {
        tag();
        trackAccount();
        addAccountItems();
      }, 1500);
    }
  }

  if (document.body) start();
  else window.addEventListener("DOMContentLoaded", start);
})();
