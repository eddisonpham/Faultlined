/*
 * Faultlined client runtime. One file, no dependencies, no build step (ADR 0014).
 *
 * Everything here is progressive enhancement: the server renders a complete,
 * readable page, and this script only makes it live. With JavaScript disabled
 * every number still shows and every control is a real form submission.
 *
 * Two jobs, in the order they matter when something breaks: keep the data fresh
 * without hammering the server, and never let a network failure look like a
 * healthy quiet system. A stale read and a calm read are different states and
 * must not render the same way. The rest is bench-instrument convenience.
 *
 * Not here: anything that decides what the user sees as *data*. Rendering
 * belongs to the server (web/pages.py), so there is one renderer, not two.
 */
(function () {
  "use strict";

  var doc = document;
  var deadlineTimer = null;

  function $(sel, root) { return (root || doc).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }

  /* ------------------------------------------------------------------ theme */

  var THEMES = ["vt220", "amber", "github-dark", "monochrome"];
  var THEME_KEY = "faultlined.theme";

  function readStoredTheme() {
    try { return window.localStorage.getItem(THEME_KEY); } catch (e) { return null; }
  }

  function storeTheme(name) {
    try { window.localStorage.setItem(THEME_KEY, name); } catch (e) { /* private mode */ }
  }

  /*
   * Theme switching has to swap the *stylesheet*, not just an attribute: each
   * theme is a separate vendored file. So we pre-load the candidate and only
   * commit once the new sheet is actually parsed, which means a missing theme
   * file leaves the working theme in place instead of blanking the page.
   *
   * The probe must be removed once it has done its job. It is appended to the
   * end of <head>, which is *after* faultlined.css, and a stylesheet left
   * there wins the cascade over our own sheet: the amber and vt220 themes put a
   * phosphor `text-shadow` on every readout, so the probe silently switched the
   * vendor glow back on for the whole session and it was reported as a glow
   * that a theme change made worse. The glow is off by a rule in
   * faultlined.css, and that rule only holds while exactly one theme sheet is
   * loaded before it.
   */
  function applyTheme(name) {
    if (THEMES.indexOf(name) < 0) return;
    var link = $('link[data-de-theme]');
    if (!link) return;
    var href = "/ui/vendor/terminal-ui/theme-" + name + ".css";
    if (link.getAttribute("href") === href) return;
    var probe = doc.createElement("link");
    probe.rel = "stylesheet";
    probe.onload = function () {
      probe.remove();
      doc.documentElement.setAttribute("data-theme", name);
      link.setAttribute("href", href);
    };
    probe.onerror = function () { probe.remove(); };
    probe.href = href;
    doc.head.appendChild(probe);
  }

  function initTheme() {
    var picker = $("[data-theme-select]");
    if (picker) {
      picker.hidden = false;
      picker.addEventListener("change", function () {
        applyTheme(picker.value);
        storeTheme(picker.value);
      });
    }
    var stored = readStoredTheme();
    if (stored && THEMES.indexOf(stored) >= 0) applyTheme(stored);
  }

  /* ------------------------------------------------------------------ clock */

  function initClock() {
    var el = $("[data-clock]");
    if (!el) return;
    var tick = function () {
      var d = new Date();
      el.textContent = d.toISOString().slice(11, 19) + " UTC";
    };
    tick();
    window.setInterval(tick, 1000);
  }

  /*
   * The deadline countdown. The server already rendered the remaining budget;
   * this only keeps it moving between polls so a long-running job does not look
   * frozen. Purely presentational - the server still owns the timeout.
   */
  function initDeadlines() {
    var els = $$("[data-deadline]");
    if (!els.length) return;
    var render = function () {
      els.forEach(function (el) {
        var deadline = Number(el.getAttribute("data-deadline")) * 1000;
        if (!isFinite(deadline)) return;
        var left = Math.round((deadline - Date.now()) / 1000);
        if (left <= 0) {
          el.textContent = Math.abs(Math.round(left / 60)) + "m over";
          el.className = "text-error";
        } else {
          el.textContent = Math.round(left / 60) + "m left";
        }
      });
    };
    render();
    // One interval for the page, re-pointed at the current nodes. This runs
    // again after every poll write, and a fresh setInterval per call would
    // leave the old ones ticking over detached nodes forever.
    if (deadlineTimer) window.clearInterval(deadlineTimer);
    deadlineTimer = window.setInterval(render, 15000);
  }

  /* ------------------------------------------------------------------- poll */

  /*
   * Failure policy:
   *
   *   - One failed request is not news. Two in a row is: raise the banner,
   *     back off, and mark the clock stale so nobody reads frozen numbers as
   *     current.
   *   - Backoff is exponential with a ceiling; a server that is down should not
   *     be hammered at the page's own cadence. A success resets it.
   *   - A 4xx other than 408/429 will never fix itself. Reload once, then stop.
   *   - The last good render is never discarded. An empty panel is a lie about
   *     the data, and worse than an obviously stale one.
   */
  function initPoll(root) {
    var url = root.getAttribute("data-poll");
    var base = Number(root.getAttribute("data-poll-ms")) || 5000;
    if (!url) return;

    var led = $("[data-led]");
    var clock = $("[data-clock]");
    var banner = $("[data-banner]");
    var bannerMsg = $("[data-banner-msg]");
    var timer = null;
    var failures = 0;
    var lastGood = Date.now();
    var stopped = false;
    /* The exact bytes of the fragment currently on screen. Seeded from the
       server-rendered DOM so the first poll is compared like every other one. */
    var shown = root.innerHTML;

    function setLed(state, label) {
      if (!led) return;
      led.setAttribute("data-state", state);
      // Only surfaced when it is news. A permanently-visible "live" badge
      // stopped being a status indicator and became a logo.
      if (state === "ok") led.setAttribute("hidden", "");
      else led.removeAttribute("hidden");
      var text = $("[data-led-text]");
      if (text) text.textContent = label;
    }

    function setStale(stale) {
      if (clock) clock.setAttribute("data-stale", stale ? "1" : "0");
    }

    function showBanner(message) {
      if (!banner) return;
      bannerMsg.textContent = message;
      banner.setAttribute("data-visible", "1");
    }

    function hideBanner() {
      if (banner) banner.setAttribute("data-visible", "0");
    }

    function announce(message) {
      var live = $("[data-live]");
      if (live) live.textContent = message;
    }

    function schedule(delay) {
      if (stopped) return;
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(tick, delay);
    }

    function tick() {
      // A hidden tab is not a reason to keep the network warm, and a page
      // restored from the background is usually minutes stale.
      if (doc.hidden) {
        setLed("paused", "paused");
        schedule(base);
        return;
      }
      if (window.navigator.onLine === false) {
        failures += 1;
        setLed("down", "offline");
        var offline = "Browser reports no network connection. Showing the last good render.";
        showBanner(offline);
        setStale(true);
        // Announced too: the banner is a visual hazard stripe, and a screen
        // reader user has no other way to learn the numbers on screen are old.
        announce(offline);
        schedule(Math.min(base * Math.pow(2, failures - 1), 60000));
        return;
      }
      window.fetch(url, { headers: { "X-Fragment": "1" }, cache: "no-store" })
        .then(function (response) {
          if (!response.ok) {
            var err = new Error("HTTP " + response.status);
            err.permanent = response.status >= 400 && response.status < 500 &&
              response.status !== 408 && response.status !== 429;
            throw err;
          }
          return response.text();
        })
        .then(function (html) {
          if (html === null || html === "") return;
          /* Write only when the payload genuinely differs, comparing against
             the last payload applied rather than `root.innerHTML`: the browser
             re-serialises the DOM, so that comparison is never equal and the
             guard would never fire. */
          if (html !== shown) {
            root.innerHTML = html;
            shown = html;
            // Re-derive what the replaced nodes owned: countdowns and stagger
            // indices. Only a write invalidates them, so a poll that changed
            // nothing does not walk the DOM to recompute identical values.
            initDeadlines();
            stagger();
            // A hairline colour lift, not an opacity dip, so "updated" stays
            // distinguishable from "something is wrong".
            root.setAttribute("data-changed", "");
            window.setTimeout(function () {
              root.removeAttribute("data-changed");
            }, 320);
          }
          lastGood = Date.now();
          var wasBroken = failures > 0;
          failures = 0;
          hideBanner();
          setLed("ok", "live");
          setStale(false);
          // Announce the recovery, not the success: a page that has been quietly
          // wrong for a minute needs to say it is right again, otherwise the
          // operator cannot tell the difference between "fixed" and "never broke".
          if (wasBroken) announce("Live updates restored.");
          schedule(base);
        })
        .catch(function (err) {
          failures += 1;
          if (err && err.permanent && failures > 1) {
            stopped = true;
            setLed("down", "failed");
            var dead = "This panel stopped updating (" + err.message +
              "). Reload the page to retry.";
            showBanner(dead);
            setStale(true);
            announce(dead);
            return;
          }
          var age = Math.round((Date.now() - lastGood) / 1000);
          setLed(failures > 1 ? "down" : "retry", failures > 1 ? "stale" : "retry");
          var message = "Cannot reach the server (" + failures + " failed" +
            (failures > 1 ? ", last good " + age + "s ago" : "") +
            "). Retrying; showing the last good render.";
          showBanner(message);
          setStale(true);
          announce(message);
          schedule(Math.min(base * Math.pow(2, failures - 1), 60000));
        });
    }

    // Coming back to the tab is a request for current data, not a reason to
    // wait out the remaining backoff.
    doc.addEventListener("visibilitychange", function () {
      if (!doc.hidden && Date.now() - lastGood > base) {
        if (timer) window.clearTimeout(timer);
        failures = 0;
        stopped = false;
        schedule(0);
      }
    });

    window.addEventListener("online", function () {
      failures = 0;
      stopped = false;
      schedule(0);
    });

    var retry = $("[data-banner-retry]");
    if (retry) {
      retry.addEventListener("click", function () {
        failures = 0;
        stopped = false;
        hideBanner();
        setLed("retry", "retry");
        schedule(0);
      });
    }

    schedule(base);
  }

  /* -------------------------------------------------------------- shortcuts */

  /*
   * Number keys jump to a section. The hint is already printed next to each nav
   * link, so the shortcut is discoverable without a help modal. Guarded against
   * firing inside a form control, which is where a user typing "3" must get a
   * "3".
   */
  function isTyping(el) {
    if (!el) return false;
    var tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "select" || tag === "textarea" || el.isContentEditable;
  }

  function initShortcuts() {
    doc.addEventListener("keydown", function (event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTyping(event.target)) return;
      var link = $('a[data-key="' + event.key + '"]');
      if (link) {
        event.preventDefault();
        window.location.href = link.getAttribute("href");
        return;
      }
      if (event.key === "r" || event.key === "R") {
        var retry = $("[data-banner-retry]");
        if (retry) { event.preventDefault(); retry.click(); }
      }
    });
  }

  /* ------------------------------------------------------------------ copy */

  /*
   * Click any id/hash/meter to copy it. Episode ids and content hashes are the
   * things a user actually pastes into a bug report, and selecting a truncated
   * one by hand is a reliable way to paste the wrong 12 characters.
   */
  function initCopy() {
    doc.addEventListener("click", function (event) {
      var target = event.target.closest ? event.target.closest("[data-copy]") : null;
      if (!target) return;
      var text = target.getAttribute("data-copy");
      if (!text) return;
      if (event.shiftKey) return; // shift-click still selects the text
      var done = function () {
        var original = target.getAttribute("title") || "";
        target.setAttribute("title", "copied");
        window.setTimeout(function () {
          if (original) target.setAttribute("title", original);
          else target.removeAttribute("title");
        }, 900);
      };
      if (window.navigator.clipboard && window.isSecureContext) {
        window.navigator.clipboard.writeText(text).then(done, function () { /* denied */ });
      } else {
        // http:// on a LAN address is not a secure context, and this app is
        // expected to be reached that way.
        var scratch = doc.createElement("textarea");
        scratch.value = text;
        scratch.setAttribute("readonly", "");
        scratch.style.position = "fixed";
        scratch.style.opacity = "0";
        doc.body.appendChild(scratch);
        scratch.select();
        try { doc.execCommand("copy"); done(); } catch (e) { /* nothing to do */ }
        scratch.remove();
      }
    });
  }/* ---------------------------------------------------------- page transition */

  /*
   * A cross-fade between pages, on the Material 3 emphasized pair. Deliberately
   * not a slide: a horizontal slide implies the new page is spatially to the
   * right of the old one, and a fade says "the content changed" and nothing more.
   *
   * The class is added to <body> and the navigation is then allowed to proceed.
   * A same-document link, a modified click, or a cancelled navigation all leave
   * the class behind, so the page can never end up invisible; `pageshow` and a
   * short timer are the backstops.
   *
   * This is the only thing that moves during a navigation. There is no progress
   * rule, no sweep, no spinner: the document is server-rendered, so anything
   * drawn during the wait is decoration drawn over a page that is already
   * correct, and it was reported as a glow. The browser's own load state is the
   * progress indicator, and it is the honest one.
   */
  function initTransition() {
    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
    doc.addEventListener(
      "click",
      function (event) {
        if (reduce.matches) return;
        if (event.defaultPrevented || event.button !== 0) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        var link = event.target.closest ? event.target.closest("a[href]") : null;
        if (!link) return;
        var href = link.getAttribute("href");
        if (!href || href.charAt(0) === "#" || link.target === "_blank") return;
        if (link.hasAttribute("download")) return;
        var url;
        try { url = new URL(href, window.location.href); } catch (e) { return; }
        if (url.origin !== window.location.origin) return;
        if (url.pathname === window.location.pathname && url.search === window.location.search) return;
        doc.body.classList.add("de-leaving");
      },
      true
    );
    // Any way the transition ends other than a fresh document, un-hide.
    window.addEventListener("pageshow", function () {
      doc.body.classList.remove("de-leaving");
    });
    window.setTimeout(function () {
      if (doc.body.classList.contains("de-leaving") && doc.visibilityState === "visible") {
        doc.body.classList.remove("de-leaving");
      }
    }, 400);
  }

  /* ------------------------------------------------------------------ boot */

  /*
   * Stagger the panel entrance. The index is applied here rather than in the
   * templates so adding a panel cannot silently reset the order, and so the
   * stagger is capped - a page with twenty panels should not take a second to
   * finish arriving.
   */
  function stagger() {
    var panels = $$(".de-section");
    var cap = 8;
    panels.forEach(function (panel, i) {
      panel.style.setProperty("--i", Math.min(i, cap));
    });
  }

  function boot() {
    initTheme();
    stagger();
    // The entrance animation is gated on html:not([data-booted]). Flip it after
    // the first frame so the entrance plays once and never again - without it
    // every poll would replay it, because a poll replaces the elements and a
    // new element restarts its animation.
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        doc.documentElement.setAttribute("data-booted", "");
      });
    });
    initClock();
    initDeadlines();
    $$("[data-poll]").forEach(initPoll);
    initShortcuts();
    initCopy();
    initTransition();
  }

  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
