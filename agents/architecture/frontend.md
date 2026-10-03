# Frontend — Scope and API Boundary

The *mechanism* was settled for MVP in [ADR 0014](../decisions/0014-minimal-ui-server-rendered.md):
server-rendered HTML plus vanilla-JS polling, no framework and no build step. The visual design and the
failure-handling pass happened later, in [ADR 0021](../decisions/0021-frontend-instrument-pass.md), which
kept ADR 0014's mechanism and changed what the UI is *for*. Style mandate (spec §4): clean, restrained,
technically credible — a purpose-built engineering tool, not a generic dashboard.

**Implemented so far:** Status/Overview, Jobs (list + detail), Artifacts, Episodes (list +
detail), Insights, Metrics, Failures, Slices, Incidents, Clusters, Schema, Builds (list + detail
with the lineage graph), Benchmarks, and Experiments, at `/ui`, `/ui/jobs`, `/ui/jobs/{id}`,
`/ui/artifacts`, `/ui/episodes`, `/ui/episodes/{id}`, `/ui/insights`, `/ui/metrics`, `/ui/failures`,
`/ui/slices`, `/ui/incidents`, `/ui/clusters`, `/ui/schema`, `/ui/builds`, `/ui/builds/{hash}`,
`/ui/benchmarks`, and `/ui/experiments`. Polling re-requests the same page with `X-Fragment: 1` and
swaps the returned HTML, so Python stays the only renderer. Runs (workload run records) remains
stage-4 work.

## Scope

Engineering-facing UI for the personas in [../spec/problem.md](../spec/problem.md) §3. MVP (definition-of-done stage 2)
needs only the **Jobs / Artifacts / Status** slice; the rest lands by Production Baseline (stage 3).

## Information architecture (pages)

| Page | Content | Stage | Primary persona |
|---|---|---|---|
| **Status / Overview** | Health: queue depth, workers, GPU/CPU/disk telemetry, recent failures | MVP | platform |
| **Jobs** | List (filters: type, state, time), detail with state transitions, attempts, logs-by-correlation-id, cancel action | MVP | all |
| **Artifacts** | Browse content-addressed artifacts, sizes, references | MVP | data |
| **Episodes** | Curation index (state + quality flags: jerky/stalled/short/long), detail: channels, motion-quality panel (ADR 0018), validation result + reason codes | **Implemented** (run-intelligence slice; FR-005 query builder still Baseline) | data, ML |
| **Insights** | Dataset curation view: length histogram, speed distribution, cross-episode variance heat matrix, outlier/removal-candidate lists | **Implemented** | data, ML |
| **Metrics (live telemetry)** | Sparkline traces per metric, API-route latency table (p50/p95/p99), stage + catalog latency, worker heartbeat | **Implemented** | platform |
| **Failures** | Validation failures grouped by reason code; job failures; revalidate/cancel actions | **Implemented** (read view; revalidate/cancel actions still Baseline) | data |
| **Slices** | Named curation filters, member counts, links to each slice's curated manifest | **Implemented** | data |
| **Incidents** | The notifier's queue: severity badges, occurrence counters, per-incident evidence line, ack/resolve, a monitor-health strip, and a rendered notify preview | **Implemented** (ADR 0020) | platform, data |
| **Datasets & Builds** | Builds list; build detail: manifest view, **lineage graph** (backwards to episodes, forwards to runs) | **Implemented** (`/ui/builds`) | ML, eval |
| **Benchmarks** | Committed baselines (P50, throughput, trials, commit, input hash) and the runs recorded on this machine, newest first, with any workload that has no committed baseline named | **Implemented** (`/ui/benchmarks`) | platform |
| **Experiments** | Every `agents/experiments/` record: id, title, status, date, author, and the record's own Result prose | **Implemented** (`/ui/experiments`) | platform, all |
| **Runs** | Workload run records: provenance fields, metrics | Stage 4 | eval |

## API boundary rules

- The UI consumes **only** `/api/v1`-shaped read models. As built (ADR 0014) the server-rendered pages call the
  catalog repository in the same process and serialize the same shapes the versioned API serves; external clients
  use `/api/v1`. No client-side DB or filesystem access, ever.
- Mutations limited to: submit jobs, cancel jobs, trigger revalidation, trigger builds/runs, and acknowledge or
  resolve an incident. Everything else is read-only. The incidents page is read-only in its markup; ack/resolve
  are POST endpoints, and the page surfaces the queue and the monitor's own health without a mutation.
- Live updates in v1: **polling** (jobs/episodes lists 3–5 s, metrics 5 s, insights 10 s, detail pages static;
  backoff when tab hidden). SSE/WebSocket is a deferred trigger (data-flow.md §5).
- Charts read `/api/v1/metrics` — the same metric schema as benchmarks/runtime (ADR 0008), so UI, benchmarks, and
  experiment records never disagree.
- **Error rendering splits by caller, not by content** (ADR 0021). A `/ui/...` path returns a full HTML error
  page carrying the status, the detail and the `correlation_id`, in the operator's selected theme; an
  `/api/v1/...` path returns the unchanged JSON problem object. Registration is against both
  `fastapi.HTTPException` and `starlette.HTTPException`, because Starlette dispatches on the most specific
  class in the MRO and the router raises the FastAPI subclass.
- Pagination: cursor-based everywhere; the UI never asks for "all rows".

## Colour: one status ramp per theme

Saturated primaries are reserved for the single thing on screen that must be seen. A status word
is not that thing. The vendored `--fine-use-success` is `#00ff00` and `core.css` applies
`.text-success` at full weight, which put one neon "ok" in a row of grey numbers and made it read
as a highlighted button.

So inline status **words** use a dedicated ramp (`--de-ok / --de-warn / --de-err / --de-info /
--de-muted`), set per theme in `faultlined.css`. The hues come from **Okabe-Ito** — published 2008,
still the most-cited colour-blind-safe set — then pulled down in chroma and toward each theme's own
background so they sit *in* the palette rather than on top of it:

| Theme | ok | warn | err | note |
|---|---|---|---|---|
| vt220 | `#6fae8f` | `#c9a15c` | `#c9776a` | desaturated toward the phosphor |
| amber | `#9a9a52` | `#c99244` | `#c26a4a` | greens pulled to olive; the background is already warm |
| github-dark | `#57ab8a` | `#c69026` | `#d1705b` | the only cool neutral ground, so the most colourful |
| monochrome | `#b8b8b8` | `#d0d0d0` | `#ffffff` | **weight** carries the alarm, not hue |

Two rules follow from this and are worth not undoing:

- **No ramp uses the theme accent.** Otherwise "this number is coloured" and "this is interactive"
  compete for the same signal.
- **The vendored `.status-*` chip components keep their saturated fill.** That is correct for a
  filled badge you are meant to read as a unit. The ramp is for inline words only.

Monochrome is exempt from any saturation bound on purpose; a test asserts this explicitly rather
than quietly loosening the threshold for everyone.

## Live updates

The poll replaces the innerHTML of `.de-live`, the innermost region on the page. Two rules follow
from that and are load-bearing.

**Compare against the last payload applied, never against `root.innerHTML`.** The browser
re-serialises the DOM, so the response text and the serialised DOM differ in entity escaping and
self-closing tags regardless of what the server sent — the comparison is never equal, so the guard
never fires and every poll rewrites the region. That is invisible as a *bug* and obvious as a
symptom: the page pulses once a poll period.

**Nothing may animate on content that refreshes.** A replaced element is a *new* element, and a
new element restarts its CSS animation from 0%. So the page entrance is gated on
`html:not([data-booted])`, a one-shot attribute `app.js` sets after the first frame, and the
"this region updated" cue is a hairline colour shift rather than an opacity dip — a dip in
opacity on live data is indistinguishable from a fault. Measured over 11 s of polling: 0 opacity
dips, 0 navigations, constant node count.

## Loading and navigation feedback

The document is server-rendered, so by the time any script runs the content is already there.
**There is no page-load spinner, and there must not be one**: an in-flow indicator on a finished
page claims work that was never outstanding, and worse, occupies real vertical space and pushes
the finished page down the viewport.

The spring sweep is therefore shown *only* while a navigation is genuinely in flight, as an
out-of-flow overlay (`position: fixed`) hidden in the served markup. Note that the class rule
needs its own `.de-sweep-panel[hidden] { display: none }` — `display: grid` at class specificity
outranks the UA rule for the `hidden` attribute, so without it the panel is a full-viewport
overlay on every settled page.

## Tables

Each data plate is its own scroll region on both axes. The horizontal case is the obvious one
(the Episodes table is ten columns). The vertical case is load-bearing: `overflow-x: auto`
already forces `overflow-y` to compute to `auto`, so the plate is a scrollport whether or not it
was intended to be one.

**A sticky header is sticky against its nearest scrollport.** With the plate as that scrollport,
`top` is measured from the top of the table, not the top of the page, so the offset is `0`. It
must not be given a nav-relative offset: the nav's height is theme- and viewport-dependent, and
depending on it is what put the header 45 px down inside its own row.

Header and cell share one line-box height (`--de-row-line`) and one padding
(`--de-cell-pad`) as *lengths*, not ratios — a unitless ratio scales with each cell's own
font-size, which reintroduces exactly the baseline skew that made labels read as belonging to the
row they overlapped.

## Motion

Every duration and easing curve is a **published Material Design 3 v0.192 token**, read from
`material-web/tokens/versions/v0_192/_md-sys-motion.scss` rather than written from memory —
emphasized `cubic-bezier(0.2, 0, 0, 1)`, emphasized-decelerate `cubic-bezier(0.05, 0.7, 0.1, 1)`,
standard-accelerate `cubic-bezier(0.3, 0, 1, 1)`, and the duration scale.

The sweep indicator is the exception and is genuinely simulated: a damped harmonic oscillator
integrated per frame with semi-implicit Euler at a fixed 1/120 s substep, the same ODE Framer
Motion and Popmotion solve. `k=170 c=14 m=1` gives a damping ratio of **0.537** — ~13.5% overshoot,
settling in ~570 ms. The damping ratio is the only number that decides the character, so it is
named and asserted in a test. (A first pass used `c=22` → ζ=0.85, which is nearly critically
damped: the overshoot fell to 0.1% and it was indistinguishable from a CSS ease-out.)

Page transitions are a cross-fade on the M3 emphasized accelerate/decelerate pair — deliberately
not a slide, because a horizontal slide implies a spatial relationship between pages that this
app has no use for.

**Every animation is declared inside `@media (prefers-reduced-motion: no-preference)`.** Not
declared-then-cancelled. The cancel-it-afterwards form reads as correct and silently is not: the
animation is still declared, and it still runs. A test walks the stylesheet and fails if any
`animation:` sits outside such a guard.

## Failure visibility

The rule the whole UI is built around: **a dead backend and a quiet system must not look the same.** A stale
read and a healthy read are different states, and an empty panel is a stronger lie than an obviously stale
one. `web/app.js` therefore owns the policy, not just the fetch:

| Condition | LED | Clock | Banner | Panel |
|---|---|---|---|---|
| healthy | `live` (green) | current | hidden | last good |
| one failed poll | `retry` (amber, blinking) | — | hidden | last good |
| two or more | `stale` (red) | marked stale | hazard-striped, with the age of the last good render | last good |
| browser offline | `offline` (red) | marked stale | as above | last good |
| permanent 4xx (not 408/429) | `failed` (red) | marked stale | "reload to retry", loop stopped | last good |
| tab hidden | `paused` (dim) | — | hidden | last good |
| recovered | `live` | current | hidden | fresh |

Backoff is exponential with a 60 s ceiling, and a success resets it. Every failure path *and* the recovery
are written to an `aria-live` region, because the banner is a visual hazard stripe a screen-reader user
would otherwise never learn about. Coming back to a hidden tab triggers an immediate refresh rather than
waiting out the remaining backoff.

The last good render is never discarded. That is the property the whole table exists to protect.

## Accessibility

Mechanical properties, each asserted by a test in `tests/unit/test_web_frontend.py`:

- A skip link and a `<main id="main">` landmark, and the skip link is the first focusable element in the document.
- A `<caption>` on every table; a scroll container with `tabindex="0"` and a `role="region"` label so a
  keyboard user can pan a wide table at all.
- `role="img"` with a real `aria-label` on every meter and every chart. A sparkline is data, not decoration.
- Focus rings are restyled, never removed. `outline: none` appears nowhere in the stylesheet.
- Every animation sits behind `@media (prefers-reduced-motion: no-preference)`.
- **Colour is never the only signal.** Every severity carries a word and a distinct glyph as well as a hue;
  the LED state is text (`live` / `retry` / `stale` / `offline` / `failed`) as well as a colour.
- Controls work with scripting disabled: cancel, filters and the theme picker are real form submissions.

Not yet done, and worth saying plainly: a pass with a real screen reader, and a colour-blindness check with
an actual operator. What is committed is the mechanical set plus a headless-browser check of computed styles.

## Boundaries with the backend

| Concern | Owned by |
|---|---|
| Data shape, validation, error codes | API (Pydantic/OpenAPI) |
| Client state, fetching, caching | `web/app.js` — the poller, the theme, the clock. It renders no data: the single `innerHTML` write in the file is the poll swap, so there is exactly one renderer |
| Visualization components (time series, lineage graph, tables) | UI; data contracts stable in `/api/v1/metrics` and lineage endpoints |
| Auth | None in v1 (localhost); UI assumes trusted local user (ADR 0009) |

## Design language

The visual identity is a **bench instrument**, not a dashboard. ADR 0021 is the source; the vocabulary is
below so the next contributor does not have to reverse-engineer it from the CSS.

- **Machined, not decorated.** Panels are bezels with a corner screw mark and an inset edge highlight. Section
  headings are silkscreen legends: small, wide-tracked, uppercase. A faceplate graticule sits behind the page.
- **Readouts, not cards.** A row of instrument readouts (`dt` legend, large tabular `dd` value, unit in
  `small`) across the top of a panel. Numbers are tabular so a column aligns.
- **Hazard striping is reserved.** The only striped element in the whole UI is the failure banner, because it
  means the instrument itself is unreliable and the numbers on screen may be stale. Using it anywhere else
  would spend the one signal that means "this panel is lying to you".
- **The notify/queue split is drawn, not just labelled.** On the Incidents page an incident that would wake
  the owner carries a left edge on its row, so the two kinds are separable without reading the Channel column.
- **Display face:** vendored [Departure Mono](../implementation/departure-mono-vendored.md) (SIL OFL) on
  headings, numeric readouts, meters, clocks, chart chrome; body prose stays on the vendored terminal-ui
  monospace stack, which is what makes a column of numbers align.
- **Charts are oscilloscope traces:** inline SVG (`<polyline>`/`<rect>`/`<circle>` only — no chart library, no
  runtime fetch) on a dotted graticule, phosphor accent from the active theme's `--fine-use-*` variables, so all
  four vendored themes stay coherent.
- **ASCII meters** (`[###.....] 42%`) for loads, stall ratios, and per-dim sigma — the terminal vocabulary is kept
  where it communicates faster than a graphic. Each carries an `aria-label`, because it is a graphic made of text.
- **Heat matrix** for cross-episode per-dim variance: CSS-grid intensity cells (`color-mix` on the accent), hover
  for exact values, horizontal scroll with a themed thin scrollbar and a keyboard-reachable container.
- **Vendored theme hooks are load-bearing.** Markup carries `fine-use-app`, `fine-use-component`,
  `fine-use-data-table`, `current-value`, `block-item` and `fine-use-focusable`, because the vendored theme
  sheets define their scanlines, phosphor glow and panel treatment against exactly those class names. A bridge
  block at the end of `faultlined.css` narrows them to the properties the vendor sets, so the theme's
  personality wins and our geometry does not fight it. Do not remove the hooks or the bridge: without them all
  four themes collapse to plain text.
- **Click to copy on every identifier.** Episode ids, job ids, correlation ids, profile hashes and artifact
  hashes are truncated to keep columns narrow, and a truncated id pasted into a bug report is worse than no id.
  `data-copy` carries the full value; shift-click still selects normally.
- Escaping is owned by `web/pages.py` (`html.escape` on every dynamic value, including inside `data-copy`
  attributes); themes are the four vendored ones, anything else falls back to `vt220`. `theme_or_default()` is
  applied on the error path too, so a bad query string cannot emit a `<link>` to a stylesheet that is not there.

## Non-goals (UI)

No dataset editing/curation authoring beyond queries; no labeling tooling (deferred, requirements §7); no real-time
robot teleop; no user management.

**No Swagger page.** `docs_url` and `redoc_url` are `None` and the nav has no API link (ADR 0021): the browser
surface is the operator UI, and a generated contract viewer is not something to put in an operator's primary
navigation. `/openapi.json` remains a machine endpoint, because the committed contract-drift check needs it.

**No frontend framework.** React/Vite/TS and Svelte remain unscored per
[technology-decision-matrix.md](technology-decision-matrix.md) §9. Reconsidering a Vue/TypeScript SPA was
raised and declined for this pass: it would not have fixed the two defects that actually mattered (invisible
failure, dead theme hooks), and it would have added a bundler and a second language toolchain to a Python-only
project. Revisit when a page genuinely needs client-side state beyond "refresh this region" — Datasets & Builds
with a lineage graph is the likely trigger.

## Getting data in

The Status page leads with an **Ingest data** panel when the system has never run a job, and the
panel is the only way to submit work from the browser. `POST /ui/jobs` accepts a urlencoded body —
either a described episode or a path to a LeRobot dataset on disk.

- **Plain form, no JavaScript.** Like every other control here. It works with scripting off.
- **No new dependency.** The body is parsed with `urllib.parse`; `Form(...)` would have pulled in
  `python-multipart`, which is not worth a new runtime package for one urlencoded body on a
  dependency-free frontend (ADR 0014). A test keeps it out.
- **One validator.** The form builds the same Pydantic models the JSON endpoint uses, so it cannot
  create a job the API would refuse. Validation errors render inline with the typed values kept;
  success is a 303 to the job page, so a refresh does not queue it twice.
- **The gate yields to failure.** The panel is hidden once the system has data — except when the
  last submission was rejected, because the error is rendered inside it. Gating purely on emptiness
  means a mistyped field on a busy system returns 200 with no explanation and nothing to correct.
