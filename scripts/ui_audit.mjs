import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, existsSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = (process.argv[2] || "http://127.0.0.1:8000").replace(/\/$/, "");

const THEMES = ["vt220", "amber", "github-dark", "monochrome"];

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

const ALLOWED_ANIMATIONS = new Set(["de-blink", "de-breathe", "de-arrive", "vt220-highlight"]);

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

    if (cs.textShadow && cs.textShadow !== "none") {
      findings.push({ kind: "text-shadow", el: describe(el), value: cs.textShadow });
    }
    if (cs.filter && cs.filter !== "none") {
      findings.push({ kind: "filter", el: describe(el), value: cs.filter });
    }
    if (cs.backdropFilter && cs.backdropFilter !== "none") {
      findings.push({ kind: "backdrop-filter", el: describe(el), value: cs.backdropFilter });
    }

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

  document.body.classList.add("de-leaving");
  const leaving = getComputedStyle(document.body);
  if (leaving.filter !== "none") {
    findings.push({ kind: "leaving-filter", el: "body.de-leaving", value: leaving.filter });
  }
  if (leaving.backdropFilter !== "none") {
    findings.push({ kind: "leaving-backdrop", el: "body.de-leaving", value: leaving.backdropFilter });
  }
  document.body.classList.remove("de-leaving");

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

      await new Promise((resolve) => setTimeout(resolve, 500));
      return cdp.send("Runtime.evaluate", { expression: AUDIT, returnByValue: true }, sessionId);
    }

    let vocabularyDetail = null;
    try {
      const res = await fetch(BASE + "/ui/vocabulary");
      const html = await res.text();
      const match = html.match(/href="\/ui\/vocabulary\/entries\/([^"]+)"/);
      if (match) vocabularyDetail = "/ui/vocabulary/entries/" + match[1];
    } catch {
    }
    if (vocabularyDetail) console.log("also visiting discovered " + vocabularyDetail);
    const ALL_PAGES = vocabularyDetail ? [...PAGES, vocabularyDetail] : PAGES;

    for (const theme of THEMES) {
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
    try {
      rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    } catch {
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
