/*
 * Browser audit for the operator UI: computed styles and geometry, measured.
 *
 * Every layout and visual defect this project has fixed was invisible in the
 * source. A `max-width` that renders fine, a `top` six pixels too high, a
 * `filter: blur(3px)` that only exists during a navigation - all of them look
 * correct in a stylesheet and none of them can be asserted by a text-matching
 * unit test. So this drives a real browser and reads back what it computed.
 *
 * No dependencies, on purpose. The UI has no build step and no npm runtime
 * (ADR 0014); a dev tool that needs `npm install` to check a Python project's
 * CSS will simply not be run. Node ships a WebSocket client, and Chrome ships a
 * DevTools protocol, so the whole thing is the standard library talking to the
 * standard library. It needs Node 22+ for the global `WebSocket`.
 *
 * Usage:
 *   node scripts/ui_audit.mjs [base-url]
 *
 * Exit codes: 0 clean, 1 a check failed, 2 the server is not reachable.
 * The server is expected to be running (`just run`); this tool never starts one,
 * because a browser pointed at a server it launched would measure a different
 * system than the one an operator uses.
 */

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, existsSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = (process.argv[2] || "http://127.0.0.1:8000").replace(/\/$/, "");

/* The themes the picker offers. Must match THEMES in web/app.js; a test asserts
 * the two lists agree, because a theme nobody checks is a theme nobody sees. */
const THEMES = ["vt220", "amber", "github-dark", "monochrome"];

/* The pages the nav offers. Checked in the order an operator meets them. */
const PAGES = [
  "/ui",
  "/ui/incidents",
  "/ui/jobs",
  "/ui/episodes",
  "/ui/failures",
  "/ui/slices",
  "/ui/vocabulary",
  "/ui/insights",
  "/ui/metrics",
  "/ui/artifacts",
  "/ui/schema",
  "/ui/builds",
  "/ui/benchmarks",
  "/ui/experiments",
];

/*
 * Animations allowed to be running on a settled page. Each is either a status
 * indicator that changes on its own (the link-health LED, the clock cursor, the
 * "now" pulse on an active job) or the one-shot panel entrance that runs once
 * per document load. Anything else running unprompted is a decoration nobody
 * asked for, which is the class of defect this file exists to catch - so the
 * allowlist is explicit rather than a threshold.
 */
const ALLOWED_ANIMATIONS = new Set(["de-blink", "de-breathe", "de-arrive", "vt220-highlight"]);

/*
 * Runs inside the page. Returns findings rather than throwing, so one bad page
 * does not hide the state of the other ten.
 */
const AUDIT = `(() => {
  const findings = [];
  const describe = (el) => {
    let out = el.tagName.toLowerCase();
    if (el.id) out += "#" + el.id;
    if (el.className && typeof el.className === "string") {
      out += "." + el.className.trim().split(/\\s+/).slice(0, 3).join(".");
    }
    return out;
  };
  const all = document.querySelectorAll("*");

  for (const el of all) {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;

    // Rule 2 (no halos): no glow anywhere, by any property. This is the check
    // that catches a vendor theme's phosphor text-shadow reaching markup we
    // added, which no amount of reading the cascade will reliably reveal.
    if (cs.textShadow && cs.textShadow !== "none") {
      findings.push({ kind: "text-shadow", el: describe(el), value: cs.textShadow });
    }
    if (cs.filter && cs.filter !== "none") {
      findings.push({ kind: "filter", el: describe(el), value: cs.filter });
    }
    if (cs.backdropFilter && cs.backdropFilter !== "none") {
      findings.push({ kind: "backdrop-filter", el: describe(el), value: cs.backdropFilter });
    }

    // Nothing may sit over the page. A fixed element covering most of the
    // viewport is a loading panel, a modal, or a progress overlay; the first two
    // are forbidden by the no-JS contract and the third was removed deliberately.
    if (cs.position === "fixed") {
      const r = el.getBoundingClientRect();
      const area = r.width * r.height;
      if (area > innerWidth * innerHeight * 0.5) {
        findings.push({
          kind: "overlay",
          el: describe(el),
          value: Math.round(area / 1000) + "k px2 over the page",
        });
      }
    }
  }

  // Exactly one theme stylesheet, and it must not come after our own sheet.
  //
  // The theme switcher appends a probe link to the end of <head> to confirm the
  // new sheet parses before committing it. If that probe is left behind it lands
  // *after* faultlined.css and wins the cascade, which switched the vendored
  // phosphor glow back on for every readout - invisible on a fresh load, since
  // no probe is created until a theme is chosen. A duplicate sheet is the
  // signature of that bug, and it is measurable from the page.
  const themeSheets = [...document.querySelectorAll('link[rel="stylesheet"]')]
    .filter((l) => (l.getAttribute("href") || "").includes("/vendor/terminal-ui/theme-"));
  if (themeSheets.length !== 1) {
    findings.push({
      kind: "theme-sheet",
      el: "head",
      value: themeSheets.length + " theme stylesheets (expected 1)",
    });
  }
  const sheets = [...document.querySelectorAll('link[rel="stylesheet"]')];
  const ours = sheets.findIndex((l) => (l.getAttribute("href") || "").includes("faultlined.css"));
  const theirs = themeSheets.length ? sheets.indexOf(themeSheets[0]) : -1;
  if (ours >= 0 && theirs > ours) {
    findings.push({
      kind: "cascade-order",
      el: "head",
      value: "the theme sheet loads after faultlined.css and overrides it",
    });
  }

  // The outgoing page during a navigation. This state exists only between a
  // link click and the new document, so a check that only ever loads a page
  // cannot see it - which is exactly how a 3px blur of the whole page survived
  // review and was only reported by an operator clicking a link.
  document.body.classList.add("de-leaving");
  const leaving = getComputedStyle(document.body);
  if (leaving.filter !== "none") {
    findings.push({ kind: "leaving-filter", el: "body.de-leaving", value: leaving.filter });
  }
  if (leaving.backdropFilter !== "none") {
    findings.push({ kind: "leaving-backdrop", el: "body.de-leaving", value: leaving.backdropFilter });
  }
  document.body.classList.remove("de-leaving");

  // A sticky column header that is not aligned with the row it labels covers
  // the data. Measured against its own row, because that is the box it is
  // sticky within; a nav-relative offset would be measuring the wrong thing.
  for (const th of document.querySelectorAll(".de-table th")) {
    const tr = th.closest("tr");
    if (!tr) continue;
    const a = th.getBoundingClientRect().top;
    const b = tr.getBoundingClientRect().top;
    if (Math.abs(a - b) > 1) {
      findings.push({
        kind: "sticky-header",
        el: describe(th),
        value: "th.top=" + Math.round(a) + " tr.top=" + Math.round(b),
      });
    }
  }

  // Only CSS motion declared by this app is subject to its allowlist. The
  // browser can also report an inline SVG SMIL animation as the generic name
  // "css"; that is not an animation-name authored by our stylesheet.
  for (const anim of document.getAnimations()) {
    const name = anim.animationName;
    if (typeof name === "string" && name && !ALLOWED.includes(name)) {
      findings.push({ kind: "animation", el: name, value: anim.playState });
    }
  }

  return { findings, title: document.title, url: location.pathname };
})()`.replace("ALLOWED", JSON.stringify([...ALLOWED_ANIMATIONS]));

function chromePath() {
  const fromEnv = process.env.CHROME_PATH;
  if (fromEnv && existsSync(fromEnv)) return fromEnv;
  const roots = process.platform === "win32"
    ? ["C:/Program Files/Google/Chrome/Application/chrome.exe",
       "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"]
    : ["/usr/bin/google-chrome", "/usr/bin/chromium", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"];
  for (const candidate of roots) if (existsSync(candidate)) return candidate;
  // A Playwright-managed Chromium is a reasonable last resort: the browser is
  // already on the machine, so the audit should not fail over a file path.
  const cache = join(process.env.LOCALAPPDATA || join(tmpdir(), ".cache"), "ms-playwright");
  if (existsSync(cache)) {
    for (const dir of readdirSync(cache)) {
      for (const rel of ["chrome-win/chrome.exe", "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                         "chrome-linux/chrome"]) {
        const candidate = join(cache, dir, rel);
        if (existsSync(candidate)) return candidate;
      }
    }
  }
  return null;
}

async function preflight() {
  try {
    const response = await fetch(BASE + "/api/v1/health");
    if (!response.ok) throw new Error("HTTP " + response.status);
  } catch (err) {
    console.error("Cannot reach " + BASE + ": " + err.message);
    console.error("");
    console.error("Start the app first:  just run");
    console.error("Then:                just ui-audit");
    process.exit(2);
  }
}

/* Launch headless Chrome and resolve the DevTools WebSocket endpoint. */
async function launch() {
  const binary = chromePath();
  if (!binary) {
    console.error("No Chrome or Chromium binary found. Set CHROME_PATH to one.");
    process.exit(2);
  }
  const profile = mkdtempSync(join(tmpdir(), "faultlined-ui-audit-"));
  const child = spawn(binary, [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--remote-debugging-port=0",
    "--user-data-dir=" + profile,
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });

  const endpoint = await new Promise((resolve, reject) => {
    let buffer = "";
    const timer = setTimeout(() => reject(new Error("Chrome did not report a debug port")), 20000);
    child.stderr.on("data", (chunk) => {
      buffer += chunk.toString();
      const match = buffer.match(/DevTools listening on (ws:\/\/\S+)/);
      if (match) { clearTimeout(timer); resolve(match[1]); }
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      reject(new Error("Chrome exited with code " + code));
    });
  });
  return { child, profile, endpoint };
}

/* A CDP connection: one socket, request ids, and event waiting. */
class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.next = 1;
    this.pending = new Map();
    this.listeners = [];
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        message.error ? reject(new Error(message.error.message)) : resolve(message.result);
        return;
      }
      for (const listener of this.listeners) listener(message);
    });
  }

  static async connect(endpoint) {
    const socket = new WebSocket(endpoint);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", () => reject(new Error("devtools socket failed")), { once: true });
    });
    return new Cdp(socket);
  }

  send(method, params = {}, sessionId) {
    const id = this.next++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    this.socket.send(JSON.stringify(payload));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }

  waitFor(method, sessionId, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("timed out waiting for " + method)), timeoutMs);
      const listener = (message) => {
        if (message.method === method && (!sessionId || message.sessionId === sessionId)) {
          clearTimeout(timer);
          this.listeners = this.listeners.filter((entry) => entry !== listener);
          resolve();
        }
      };
      this.listeners.push(listener);
    });
  }
}

async function main() {
  await preflight();
  const { child, profile, endpoint } = await launch();
  let failures = 0;
  let checks = 0;
  try {
    const cdp = await Cdp.connect(endpoint);
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    await cdp.send("Page.enable", {}, sessionId);
    await cdp.send("Runtime.enable", {}, sessionId);

    async function visit(url) {
      const loaded = cdp.waitFor("Page.loadEventFired", sessionId);
      await cdp.send("Page.navigate", { url }, sessionId);
      await loaded;
      // The theme swap happens on boot, and the cascade only settles once the
      // probe has been appended and committed. Half a second is generous for a
      // local stylesheet and keeps the run honest.
      await new Promise((resolve) => setTimeout(resolve, 500));
      return cdp.send("Runtime.evaluate", { expression: AUDIT, returnByValue: true }, sessionId);
    }

    /*
     * Vocabulary entry detail pages are dynamic routes, so discover one from
     * the rendered vocabulary index instead of hard-coding an entry id.
     */
    let vocabularyDetail = null;
    try {
      const res = await fetch(BASE + "/ui/vocabulary");
      const html = await res.text();
      const match = html.match(/href="\/ui\/vocabulary\/entries\/([^"]+)"/);
      if (match) vocabularyDetail = "/ui/vocabulary/entries/" + match[1];
    } catch {
      /* preflight already proved the server is reachable; treat as none */
    }
    if (vocabularyDetail) console.log("also visiting discovered " + vocabularyDetail);
    const ALL_PAGES = vocabularyDetail ? [...PAGES, vocabularyDetail] : PAGES;

    /*
     * Every page under every theme. The theme matters and is not redundant:
     * two of the four vendored sheets ship a glow rule and two do not, and the
     * stylesheet the client appends on a swap only exists once a theme has been
     * chosen. A check on the default theme would have passed while amber glowed.
     */
    for (const theme of THEMES) {
      /*
       * Render every page in a *different* theme than the one stored, so the
       * client-side swap actually runs on each load. That is not a contrivance:
       * an operator who picked a theme gets plain URLs back from the server
       * rendering the default, and the swap happens on every page they visit.
       * Loading the theme that is already active makes the swap a no-op, so the
       * vt220 pass - the one theme that ships a glow rule *and* is the server
       * default - would silently test nothing. That gap is how this bug shipped.
       */
      const seed = theme === "vt220" ? "amber" : "vt220";
      const preview = theme === "vt220" ? "amber" : "vt220";
      await visit(BASE + PAGES[0] + "?theme=" + seed);
      await cdp.send("Runtime.evaluate", {
        expression: "localStorage.setItem('faultlined.theme', " + JSON.stringify(theme) + ")",
      }, sessionId);

      for (const path of ALL_PAGES) {
        checks += 1;
        const evaluated = await visit(BASE + path + "?theme=" + seed);
        const result = evaluated.result.value;
        if (!result) {
          console.error("FAIL [" + theme + "] " + path + ": the page produced no result");
          failures += 1;
          continue;
        }
        if (result.findings.length === 0) {
          console.log("ok   [" + theme + "] " + path);
          continue;
        }
        failures += 1;
        console.error("FAIL [" + theme + "] " + path);
        for (const finding of result.findings) {
          console.error("       " + finding.kind + ": " + finding.el + " -> " + finding.value);
        }
      }
    }
  } finally {
    child.kill();
    // Chrome holds its profile open for a moment after the kill signal, and on
    // Windows that makes removal fail with EPERM. The profile is in the system
    // temp directory, so leaving it behind is harmless; losing the exit code to a
    // cleanup failure would not be.
    try {
      rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    } catch {
      /* best effort */
    }
  }

  console.log("");
  console.log(failures === 0
    ? "UI audit clean: " + checks + " page loads across " + THEMES.length + " themes."
    : "UI audit found problems in " + failures + " of " + checks + " loads.");
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("UI audit failed: " + err.message);
  process.exit(2);
});