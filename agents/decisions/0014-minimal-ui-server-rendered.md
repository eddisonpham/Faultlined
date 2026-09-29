# 0014. Build the MVP UI as server-rendered HTML with vanilla-JS polling

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** owner + frontend-engineer agent (stage 2, MVP)

## Context

Stage 2 (MVP) requires a minimal UI covering **Status / Jobs / Artifacts** (definition-of-done). Opening the
service in a browser today returns a blank page, and the only browser surface is the generated Swagger UI, which is
for developers reading the contract rather than for operating the platform.

[../architecture/frontend.md](../architecture/frontend.md) already freezes *what* the UI covers and *how* it talks to
the engine: it consumes only `/api/v1`, mutations are limited to submit/cancel/revalidate/build, live updates are
**polling** in v1 with SSE/WebSocket deferred, and pagination is cursor-based. It defers visual design and framework
selection to the phase-12 design pass, so this ADR settles only the *mechanism* needed to ship the MVP slice, not the
visual design.

The engine is a single Python process on one host with a small audience of engineers. The MVP pages read
queue state, job lists, and artifact metadata: all of it already lives in PostgreSQL behind `/api/v1`.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Server-rendered HTML + vanilla JS polling (chosen)** | No build step, no bundler, no new runtime dependency; the app stays one `just run` process; pages work with JS disabled; trivial to read and review; matches the documented polling rule exactly | No component model; re-render logic is hand-written; a later redesign would mean rewriting the views |
| React / Vue SPA | Component model, ecosystem, easier future growth | Adds a bundler, build step, `node_modules`, and a second language toolchain to a Python-only project; a second way for the UI to disagree with the OpenAPI contract; far more machinery than three read-mostly pages need |
| HTMX | Small, server-driven | Still a dependency plus attribute-driven templates; buys little over plain fetch + vanilla JS for three polling pages |
| Static site generated from the database | No server rendering | Cannot poll live state, which the Status page exists to show |

## Decision

Render the MVP pages on the server as plain HTML, and use a small amount of vanilla JavaScript to poll
`/api/v1` on the intervals the frontend specification already fixes (jobs list 3 s, detail 5 s). Add **no
frontend framework, bundler, or new runtime dependency**. Templates live in
`src/data_engine/web/` as Python-owned HTML with a single shared stylesheet, served by the existing FastAPI app.

The UI reads only `/api/v1`. It never touches PostgreSQL or the artifact store directly, per the frontend
specification's API-boundary rule. Visual design remains deferred to the phase-12 pass; this ADR deliberately does
not lock in visual style.

## Consequences

- (+) Zero new dependencies; the technology-matrix and lockfile stay as they are, and `just run` remains the single
  way to start the platform.
- (+) The MVP is reachable in a browser immediately, which is the actual gap the owner hit.
- (+) Hand-written fetch/polling is honest about the documented polling rule and is easy to verify in tests.
- (−) A richer UI later (graph views, lineage graphs, charts) will likely justify revisiting this decision;
  the documented trigger is the stage-3 Baselines/Benchmarks/Failures pages, not MVP.
- (−) Server rendering means the pages are only as correct as the read endpoints behind them, so those endpoints
  are a prerequisite and are added in the same change rather than mocked.

## Addendum (2026-09-28): vendored terminal stylesheet

The owner asked for a "low level, robotic" look and suggested using an existing template. Two were
evaluated. `terminal.css` (panic) is classless and prose-oriented, so it has nothing for dense telemetry
tables. `terminal-ui-components` (MIT, pinned at `b5d9a832eb6d631e4610cc9662b79707bb1a6bd6`) ships the
right vocabulary as a single stylesheet with **no JavaScript and no build step**, so adopting it adds a
file, not a framework. Generic admin templates (Tabler, CoreUI, AdminLTE) were rejected: they are the
dashboard look this specification rules out and they require a bundler.

Vendoring a single CSS file preserves the constraint this ADR exists to protect, so the decision stands.
Provenance, licence, and the known caveats are in
[../implementation/terminal-ui-vendored.md](../implementation/terminal-ui-vendored.md).

**Caveat worth stating plainly:** that project had 0 stars and 0 forks when pinned, and its README lists
themes that do not exist in the repository. It is unproven. It is isolated behind two routes and one
stylesheet link so it can be dropped without touching Python logic.

## Addendum (2026-09-29): the first write action

The pages were read-only, which made the lifecycle work invisible from the browser. The job
detail page now carries a cancel control, and the rule it follows is the one this ADR already
protects: **the control is a form post, not JavaScript.** The button posts to `/ui/jobs/{id}/cancel`,
which calls the same repository method as `POST /api/v1/jobs/{id}/cancel` and redirects (303) back
to the detail page. No bundler, no framework, no client-side state, and it works with scripting
disabled - which is the property the whole no-build decision was chosen for.

Two details worth recording:

- A cancel race redirects rather than surfacing a 409. The job may legitimately finish between
  render and click; the detail page already shows the authoritative state, so an error page would
  replace truth with noise for something the user did not cause.
- The button only renders for cancellable states (`queued`/`running`/`retrying`). Terminal jobs
  show a `// terminal state` note instead, so the UI never implies an action the API will reject.

The interactive-but-not-built additions are the attempt meter (`2/3` plus an ASCII bar) in the jobs
list and the deadline countdown on the detail page. The countdown is presentational only: it ticks
in the browser so a deadline does not need a poll to look alive, while the server still owns the
timeout (ADR 0015).

## Style mandate

The owner redefined the visual direction as a low-level instrument/telemetry console: monospace
throughout, hairline square panels, uppercase tracked labels, ASCII load meters, and block-glyph state
indicators. This is *more* restrained than a generic dashboard, not less, so it is consistent with the
spirit of the "restrained, technically credible" mandate; the phrase "not a generic dashboard" in
[../architecture/frontend.md](../architecture/frontend.md) is now reinforced rather than relaxed. Visual
design proper remains deferred to phase 12.

## Docs updated

- [../architecture/frontend.md](../architecture/frontend.md) (mechanism now decided; visual design still deferred)
- [../architecture/api.md](../architecture/api.md) (read endpoints the UI consumes)
- [../implementation/status.md](../implementation/status.md) (what the MVP UI does and does not cover)
