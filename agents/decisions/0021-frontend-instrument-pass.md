# 0021. Instrument UI pass: one client runtime, visible failure, and no Swagger surface

- **Status:** accepted
- **Date (UTC):** 2026-09-29
- **Deciders:** owner + frontend-engineer agent

## Context

[ADR 0014](0014-minimal-ui-server-rendered.md) settled the *mechanism* (server-rendered HTML, vanilla-JS
polling, no bundler) and deliberately deferred the visual design to a later pass. That pass had now
arrived, and looking at the running UI surfaced four problems that were not visible in the source:

1. **The vendored themes did nothing.** `theme-vt220.css` and `theme-amber.css` define their character
   against class hooks the markup never used - `.fine-use-app`, `.current-value`, `.fine-use-component`,
   `.block-item`, `.status-*`, `.terminal-cursor`. The scanline overlay, the phosphor glow and the themed
   panel treatment were all live CSS with no matching element. Every theme rendered as plain text on a
   black background.
2. **A dead backend looked like a healthy one.** The poller caught fetch errors and did nothing:
   `// keep the last good render on a transient error`. There was no banner, no staleness marker and no
   error state, so a laptop that had lost the server displayed yesterday's numbers as if they were current.
   On a platform whose whole purpose is telling an operator their pipeline is broken, this was the most
   serious defect in the UI.
3. **Errors on a UI route came back as JSON.** A mistyped `/ui/...` URL or a stale bookmark produced the
   RFC-7807 body as raw text in the browser, with no way back to the app.
4. **The nav advertised a `/docs` link** to the generated Swagger page, which is a developer contract
   viewer sitting in the operator's primary navigation.

Separately, the nine pages each carried their own inline copy of the clock and poll loop.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Instrument pass on the existing stack (chosen)** | Fixes all four defects; no new dependency, no build step, `just run` unchanged; the theme personality finally renders | Does not give a component model; the layout CSS is still a hand-maintained file |
| Rebuild the UI as a Vue/TypeScript SPA | Real components, a design system, easier future growth | Adds `node_modules`, a bundler, a second language toolchain and a second renderer to a Python-only project. It also would **not** have fixed defect 2 by itself - visibility of failure is a design decision, not a framework feature |
| Adopt the vendored class hooks in markup, but add our own visual layer on top | Same as chosen, minus the bridge | Two visual languages fighting over `text-shadow` and borders; the cascade between `core.css`, the theme and ours becomes untrackable |
| Drop the vendored stylesheet and hand-roll everything | Total control | Discards a working, licence-vendored colour/type system for a rewrite, and loses the CRT themes the owner asked for |

## Decision

Keep [ADR 0014](0014-minimal-ui-server-rendered.md)'s mechanism. This ADR makes four changes within it and
records them so the next person does not undo them by accident.

**1. Adopt the vendored hooks; keep the vendor.** Markup now carries `fine-use-app`, `fine-use-component`,
`fine-use-data-table`, `current-value`, `block-item`, `fine-use-focusable` and `theme-dropdown`. A bridge
block at the end of `faultlined.css` narrows those classes to the properties the vendor sheets also set, so
a theme's personality wins and our geometry does not fight it. The bridge is kept in one block on purpose:
scattering it makes the three-way cascade untrackable.

**2. One client runtime, and failure is visible.** `src/data_engine/web/app.js` replaces the nine inlined
script blobs. Its defining property is not the polling - it is the failure policy:

- a single failed request is not news; two in a row raises a hazard-striped banner,
- backoff is exponential with a ceiling, and a 4xx that is not 408/429 is treated as permanent and stops
  the loop rather than retrying forever,
- the clock is marked stale, and the nav LED changes state and word (`live` / `retry` / `stale` /
  `offline` / `failed`),
- **the last good render is never discarded.** An empty panel is a stronger lie than an obviously stale one,
- every failure path *and* the recovery are written to an `aria-live` region, because the banner is a
  visual hazard stripe that a screen-reader user would otherwise never learn about,
- polling pauses on a hidden tab and resumes immediately on return rather than waiting out the backoff.

**3. UI routes answer in HTML.** `/ui/...` errors render a full error page carrying the status, the detail
and the correlation id, in the theme the operator had selected; `/api/v1/...` errors keep the JSON
contract unchanged. Registered against both `fastapi.HTTPException` and `starlette.HTTPException`, because
Starlette dispatches on the most specific class in the MRO and the router raises the FastAPI subclass -
registering only the base left the exact case that matters most on the default handler.

**4. No Swagger surface.** `docs_url=None`, `redoc_url=None`, and the nav link is gone. `/openapi.json`
stays: it is a machine endpoint, and `tests/unit/test_openapi_contract.py` needs it to detect drift.

Accessibility work in the same pass, because "instrument" is not an excuse for "unusable": a skip link and
`main` landmark, a caption on every table, `aria-label` on every meter and chart, visible focus rings that
are never removed, `prefers-reduced-motion` guarding every animation, and colour never being the only
signal - every severity carries a word and a glyph as well as a hue.

## Consequences

- (+) The four vendored themes now render as designed. Verified in a real browser: `h1` resolves to
  Departure Mono with the theme's own `text-shadow`, the readouts pick up `.current-value`, the app shell
  carries the scanline.
- (+) A backend outage is now visibly different from a quiet system. Verified by killing the network under
  a live page: banner appears, LED reads `offline`, clock marks stale, the 3 rows and 10 readouts are
  retained, and the message reaches the live region.
- (+) One runtime file instead of nine inline copies; a fix to the failure banner is made once.
- (+) `/ui/nowhere` renders a real page with a retry control instead of a JSON blob.
- (−) The bridge block in `faultlined.css` is a deliberate coupling to a vendored stylesheet we do not
  control. It is commented as such, and pinned upstream, so it is visible rather than mysterious.
- (−) The vendor hook names are opaque (`.fine-use-app`, `.current-value`). They are load-bearing now, so a
  rename upstream would silently un-style the UI. A test asserts each hook is present in the markup, and
  the browser audit checks the computed styles rather than the class names alone.
- (−) Accessibility claims are only as good as the audit that produced them. A real screen-reader pass and
  a colour-blindness check with an actual operator are still outstanding; what is committed is a set of
  mechanical properties plus a headless-browser check.

## Addendum (2026-09-29): three defects found by driving a real browser

The first pass of this ADR was reviewed against screenshots and three defects surfaced that
reading the source had not. They are recorded because each one is a *measurement* error of the
kind this project is supposed to catch, and because two of them had been shipped in the
stylesheet this ADR introduced.

**Sticky column headers sat under the nav.** `.de-table th` was `position: sticky; top: 2.4rem`
- a literal guess at the nav height. Measured in Chrome, the nav is 45 px, so every sticky
header pinned 6.6 px *above* the nav's bottom edge and slid underneath it, overlapping the first
rows of the table it was labelling. The header and the data ended up in the same place. Fixed by
measuring the nav height at runtime and publishing it as `--de-nav-h`, which the header rule
consumes. A hardcoded layout constant is a bug waiting for a font change.

**Charts filled 61% of their panel.** `.de-chart` carried `max-width: 46rem`, so a 736 px trace
sat in a 1208 px bezel and the panel looked half empty. Removed; a scope screen spans its bezel.
Measured 97% after.

**The status colour was a sore thumb, and it was our doing.** The vendored
`--fine-use-success` is `#00ff00` and `core.css` applies `.text-success` at full weight, so a
single "ok" landed in a row of grey numbers at full neon saturation and read as a highlighted
button rather than as a reading. Fixed with a dedicated status ramp per theme, derived from the
**Okabe-Ito** colour-blind-safe set (published 2008, still the most-cited standard for this) and
then pulled down in chroma and toward each theme's own background. None of the four ramps uses
the theme accent, so "this number is coloured" and "this is interactive" stop competing. The
ramp is scoped to the inline status *words* only; the vendored `.status-*` chip components keep
their saturated fill, which is correct for a filled badge.

Monochrome is exempt from the saturation rule by design: its premise is that weight carries the
alarm, so its `--de-err` is `#ffffff`. A test asserts the chromatic ramps stay desaturated and
exempts that theme explicitly, rather than quietly loosening the bound.

## Addendum (2026-09-29): motion, and the two ways to get it wrong

**Use published tokens, not remembered ones.** Every duration and easing curve is the Material
Design 3 v0.192 value, read out of
`material-web/tokens/versions/v0_192/_md-sys-motion.scss` rather than written from memory:
emphasized `cubic-bezier(0.2, 0, 0, 1)`, emphasized-decelerate `cubic-bezier(0.05, 0.7, 0.1, 1)`,
standard-accelerate `cubic-bezier(0.3, 0, 1, 1)`, and the duration scale. A designer can look
these up; the agent cannot trust itself to.

**"Physics-based" has to actually be physics.** The loading indicator is a damped harmonic
oscillator integrated per frame with semi-implicit Euler at a fixed 1/120 s substep - the same
ODE Framer Motion and Popmotion solve. A CSS cubic-bezier is a *timeline*, not a simulation; it
can imitate one overshoot but cannot express "release a mass toward a target".

The constants are chosen, and the first choice was wrong. At k=170 c=22 m=1 the damping ratio is
zeta = 0.85, which is nearly critically damped: the overshoot fell to 0.1% and the "physics" was
invisible, indistinguishable from a plain ease-out. k=170 c=14 m=1 gives zeta = 0.537, ~13.5%
overshoot, 2% settling in ~570 ms. Measured in the browser, the trace goes 0 → 0.505 → settles at
0.450. **The damping ratio is the only number that decides the character, so it is named and
tested, not left implicit in three magic constants.**

**The reduced-motion guard must be opt-in, and this is a trap worth writing down.** The first
version declared the entrance animation unconditionally and switched it off inside a
`prefers-reduced-motion: reduce` block. That reads as correct and silently is not: under
`reduce` the cancellation block applies, but the animation was still *declared*, and measurement
in Chrome with `reducedMotion: "reduce"` showed `animation-name: de-arrive` still resolving.
Every animation is now declared *inside* `@media (prefers-reduced-motion: no-preference)`.
Opting in is the only form that cannot be forgotten, and a test walks the stylesheet asserting
no `animation:` declaration sits outside such a guard.

The indicator is server-rendered *visible* and hidden by script, which is the opposite of the
usual pattern. On a local server responses are frequently sub-100 ms, and a spinner shown by
script cannot be suppressed on the fast path, so it would flash on every load. The cost is one
empty box for the ~40 ms before `app.js` boots. A hard 2.6 s deadline guarantees it cannot get
stuck over the content, and reduced motion skips it entirely rather than showing it static.

## Addendum (2026-09-29): the tool that found all of this

`@playwright/mcp` (Microsoft) is registered in `.mcp.json`, and a direct Playwright script drives
the same browser for automated geometry and computed-style assertions. Reading the CSS could not
have found any of the three layout defects: a `max-width` that renders correctly and a `top` that
is 6 px too high both look correct in source. They required a browser and a ruler.

The measurement discipline that paid off: **verify the verifier first.** An early version of the
audit script compared a header's absolute `x` against the table's *width* and reported a
133 px overflow on every table on every page. That was the script's bug, not the page's - and
had it been trusted, it would have "fixed" a non-existent bug while the real 34 px header overlap
went unfixed. The reported user-visible bug and my tool's output disagreed, which is when the
tool got checked.

## Addendum (2026-09-29): three reported defects, and what they had in common

All three came from the operator, not from a test. Each had been measured and "fixed" at least
once before, and each fix addressed the symptom rather than the mechanism.

**Sticky headers were pinned to the wrong scrollport.** `overflow-x: auto` forces `overflow-y` to
compute to `auto` - one axis may not be `visible` while the other scrolls - so `.de-table-wrap`
was already a scrollport. The column header was `position: sticky` against *it*, so `top` was
measured from the top of the table rather than the top of the page. Measured on the incidents
page: `th.top = 483` inside its own `tr.top = 438`, a 45 px displacement equal to the nav offset
being applied against the wrong box. Two earlier passes had tried to correct this by measuring
the nav height more carefully (`--de-nav-h`, published by a `ResizeObserver`), which can only ever
be a better estimate of the wrong number. The plate is now explicitly a bounded scroll region
(`overflow: auto; max-height: min(70vh, 44rem)`) and the header pins to `top: 0`, which deletes
the nav coupling and the observer with it. Header and cell also share one line-box height as a
*length* (`--de-row-line: 1.08rem`): a unitless ratio scales with each cell's own font-size, which
is the skew this replaces.

**The page was flickering once a second, from a guard that could never fail.** The poller
compared the response against `root.innerHTML` and only wrote on a difference. That looks
correct and is not: the browser re-serialises the DOM, so the two strings differ in entity
escaping and self-closing tags regardless of what the server sent. The guard was always true,
every poll rewrote the region, and the refresh pulse fired on every cycle. The fix is to compare
against the last payload *applied*, seeded from the server-rendered DOM - which is also cheaper,
since it avoids serialising the whole region per poll. Verified: 110 samples across 11 s of
polling, 0 opacity dips, 0 navigations, constant node count.

**The loading panel was drawing over a page that had already loaded.** It was in normal flow, so
it occupied real vertical space and pushed the finished page down the viewport - a layout bug
wearing a costume. The document is server-rendered; by the time any script runs the content is
already there, so an indicator shown on load is claiming work that was never outstanding. It is
now an out-of-flow overlay, hidden in the served markup, shown only while a navigation is
genuinely in flight. Note the class rule needed its own `.de-sweep-panel[hidden] { display: none }`:
`display: grid` at class specificity outranks the UA rule for the `hidden` attribute, so the
panel was a full-viewport overlay on every settled page. A test now asserts the restated rule.

**A fourth defect, found by the test written for the third.** `initDeadlines()` created a
`setInterval` on every call, and it is called after every poll write - so a page left open for an
hour accumulated dozens of timers, each holding a closure over nodes that were no longer in the
document. It also could not be caught by loading the page once and looking at it. The timer is now
module-scoped and cleared before it is set. Worth recording because the sequence matters: the
poll-swap optimisation (only re-derive what a write invalidated) is what made the repeated call
*more* frequent, and following that optimisation through is what exposed the leak underneath it.

**The common thread: every one of these was a measurement, not an opinion.** None is visible by
reading the stylesheet, and two of the three had a plausible-looking fix already applied. The
`snap.mjs` warning about a "133 px header overflow" was, again, the script's arithmetic and not
the page's - confirmed by measuring the last header against the table box directly
(`overflowsBy: -1`). The value of the harness is as much in the checks it fails as in the ones
it passes.

## Addendum (2026-09-29): the operator surface does not speak in transports

The UI named its own implementation. A metrics panel was headed "API latency by route" and rendered
route templates verbatim - `/api/v1/metrics`, `/api/v1/monitoring/tick` - into table cells. Empty
states offered no way forward except an instruction to use a verb and a path: "submit one with POST
/api/v1/jobs", "run one window with POST /api/v1/monitoring/tick". The episode page linked to a raw
JSON document under the label `// episode json`.

None of that is actionable by the person reading it, and all of it is a map of the surface. Latency
is now reported **per operation, named in words** ("episode listing", "monitoring window"), via
`_operation()`, which matches the whole template rather than the last segment - the last segment is
frequently the variable part, since `/api/v1/monitoring/tick` ends in "tick". An unmapped route
degrades to its last segment with separators tidied and never falls through to the raw template, so
a newly added route cannot leak its shape into the UI by omission. A test walks every page asserting
`/api/v1` appears nowhere in the rendered body.

**One exception is deliberate.** The slice manifest stays a download, because a manifest is the
build input and downloading one is a real operator action. It is relabelled `// download manifest`
and carries `download`, so it is a named artifact rather than a hand-off to a raw path. Anything the
operator must *read* is named for the work; anything they must *fetch* may still be a file.

## Addendum (2026-09-29): the page transition did nothing

`.de-leaving` declared `transition: opacity ...` and **no rule ever changed the opacity**. The
transition was a no-op: the page snapped and only the indicator moved. A transition property with
nothing to transition is invisible in review and in a screenshot, which is precisely why it
survived a round of measurement-driven fixes to the same screen.

The outgoing page now recedes for real - opacity 1 → 0.25 and a 3px blur, measured over ~240 ms -
and the indicator changed shape to match what it is. It was a bordered box floating in the middle of
the viewport, which read as a *dialog*: it occluded the page it was describing and implied a
decision the operator might have to make. It is now a 2px rule pinned to the top edge, filling
left to right. A progress rule occludes nothing and is where someone already looks after clicking.

The blur is the load-bearing part, not decoration. A fade alone reads as a rendering glitch; a
simultaneous defocus reads as "this is being replaced".

## Addendum (2026-09-29): the form, and what it cost to add one

The UI had no way to put data *into* the system. Ingest was a `curl` in the README, and the
documented way in (`/docs`) had itself been removed - so a new user had no path at all. `POST
/ui/jobs` closes that, and the first-run panel on Status hosts it.

**No new dependency.** `Form(...)` is the obvious way to read a form body, and it requires
`python-multipart` - a new runtime package on the dependency-free path ADR 0014 is built around,
to save three lines of `urllib.parse`. The body is parsed with the standard library instead. A test
asserts `python-multipart` stays out of `pyproject.toml`, because this is exactly the kind of
regression that arrives innocently.

**One validator, two doors.** The form builds the same Pydantic models the JSON endpoint uses
(`SyntheticEpisode`, `SourceIngestPayload`) rather than assembling a dict and hoping. A form is a
second front door to the same door; a second front door with its own weaker validation is how a UI
ends up able to create jobs the API would refuse. An unhandled `ValidationError` is rendered as
`_first_message(exc)` - the first failure, with the Pydantic type code and context dict stripped
off, because a raw Pydantic error is right for a machine and useless on a form.

**A rejected submission re-renders with the typed values intact.** Losing what someone typed
because one field was wrong is the fastest way to make a form feel hostile. Success is a 303 to
the job page, so a refresh does not queue the job twice.

**The panel is gated on a genuinely empty system** - no jobs *and* no episodes. On a system that
has never run it is the most important panel on the page; once there is anything to look at it is
a distraction sitting above the numbers people came for, so it goes away. Both halves are asserted.

**One escaping rule per parameter.** `_empty()` escapes `hint` and renders `link` as markup, so an
author's `<a>` works and a value cannot reach the page raw. A single "trusted" flag would have
made both failures available at once.

## Addendum (2026-09-29): `just run` should say what is wrong

A second `just run` died with a bare `WinError 10048`. That reads like a fault in this project
rather than "a server you started earlier is still listening" - and the fix is one command the
reader has to guess. The recipe now checks the port first, using the same environment variable the
settings read, and prints the holding PID with the exact command to stop it. A preflight check is the only
kind of error handling that helps someone who does not already know the answer.

Correction (2026-09-30): the variable named here was `DE_PORT`, which the settings never read. The
preflight therefore checked 8000 whatever the operator set and printed an override the server would
ignore. It reads `DE_API_PORT` (the `api_port` setting) now; see F19 in the failure-mode catalog.

**The first attempt at that check was itself a bug, and a worse one.** It was written as a
`#!/usr/bin/env bash` recipe with the port logic inline, which made `just` resolve the interpreter
through `cygpath` - absent from a PowerShell PATH that has Git's `bin` but not `usrin`. So the
recipe that existed to make `just run` work reliably made it fail on every machine, with a message
(`Could not find cygpath`) that describes neither the port nor the conflict. The justfile already
carries a comment about this exact trap for `windows-shell`; the lesson is that a shebang recipe is
not "just a shell script" in a cross-platform task runner.

The check is now `scripts/check_port.py`, called as a plain recipe line. Python is the right host
for it on the merits, not only as a workaround: it needs `netstat -ano` on Windows and `lsof`
elsewhere, which is three lines of Python and an unreadable conditional in a recipe. It reads the
port from the same environment the settings do, so it cannot disagree with the port the server is
about to bind; it prints nothing when the port is free, because a check that chatters on the happy
path trains people to ignore its output; and it degrades to a `findstr`/`lsof` one-liner when it
cannot identify the owning process, because a diagnosis with no remedy is the failure mode this
whole change exists to remove.
