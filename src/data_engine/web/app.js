(function () {
  "use strict";

  var doc = document;
  var deadlineTimer = null;

  function $(sel, root) { return (root || doc).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }

  var THEMES = ["vt220", "amber", "github-dark", "monochrome"];
  var THEME_KEY = "faultlined.theme";

  function readStoredTheme() {
    try { return window.localStorage.getItem(THEME_KEY); } catch (e) { return null; }
  }

  function storeTheme(name) {
    try { window.localStorage.setItem(THEME_KEY, name); } catch (e) { }
  }

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

    if (deadlineTimer) window.clearInterval(deadlineTimer);
    deadlineTimer = window.setInterval(render, 15000);
  }

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

    var shown = root.innerHTML;

    function setLed(state, label) {
      if (!led) return;
      led.setAttribute("data-state", state);

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

          if (html !== shown) {
            root.innerHTML = html;
            shown = html;

            initDeadlines();
            stagger();

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

  function initCopy() {
    doc.addEventListener("click", function (event) {
      var target = event.target.closest ? event.target.closest("[data-copy]") : null;
      if (!target) return;
      var text = target.getAttribute("data-copy");
      if (!text) return;
      if (event.shiftKey) return;
      var done = function () {
        var original = target.getAttribute("title") || "";
        target.setAttribute("title", "copied");
        window.setTimeout(function () {
          if (original) target.setAttribute("title", original);
          else target.removeAttribute("title");
        }, 900);
      };
      if (window.navigator.clipboard && window.isSecureContext) {
        window.navigator.clipboard.writeText(text).then(done, function () { });
      } else {

        var scratch = doc.createElement("textarea");
        scratch.value = text;
        scratch.setAttribute("readonly", "");
        scratch.style.position = "fixed";
        scratch.style.opacity = "0";
        doc.body.appendChild(scratch);
        scratch.select();
        try { doc.execCommand("copy"); done(); } catch (e) { }
        scratch.remove();
      }
    });
  }

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

    window.addEventListener("pageshow", function () {
      doc.body.classList.remove("de-leaving");
    });
    window.setTimeout(function () {
      if (doc.body.classList.contains("de-leaving") && doc.visibilityState === "visible") {
        doc.body.classList.remove("de-leaving");
      }
    }, 400);
  }

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
