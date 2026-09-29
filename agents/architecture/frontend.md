# Frontend — Scope and API Boundary

Visual design happens in phase 12 ([../prompts/12-frontend-design.md](../prompts/12-frontend-design.md)); the
*mechanism* was settled for MVP in [ADR 0014](../decisions/0014-minimal-ui-server-rendered.md): server-rendered
HTML plus vanilla-JS polling, no framework and no build step. Style mandate (spec §4): clean, restrained,
technically credible — a purpose-built engineering tool, not a generic dashboard.

**Implemented in stage 2 so far:** Status/Overview, Jobs (list + detail), Artifacts, Episodes (list +
detail), Insights, Metrics, Failures, Slices, and Incidents, at `/ui`, `/ui/jobs`, `/ui/jobs/{id}`,
`/ui/artifacts`, `/ui/episodes`, `/ui/episodes/{id}`, `/ui/insights`, `/ui/metrics`, `/ui/failures`,
`/ui/slices`, and `/ui/incidents`. Polling re-requests the same page with `X-Fragment: 1` and swaps the
returned HTML, so Python stays the only renderer. Datasets & Builds, Runs, and Benchmarks remain stage-3 work.

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
| **Datasets & Builds** | Builds list; build detail: manifest view, **lineage graph** (backwards to episodes, forwards to runs) | Baseline | ML, eval |
| **Runs** | Workload run records: provenance fields, metrics | Baseline | eval |
| **Benchmarks** | Result tables + charts (p50/p95/p99, throughput), baseline diffs, regression flags | Baseline | platform |

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
- Error rendering mirrors problem-object codes (api.md); the UI shows `correlation_id` prominently for support/debug.
- Pagination: cursor-based everywhere; the UI never asks for "all rows".

## Boundaries with the backend

| Concern | Owned by |
|---|---|
| Data shape, validation, error codes | API (Pydantic/OpenAPI) |
| Client state, fetching, caching | UI (framework's query layer; choice at phase 12) |
| Visualization components (time series, lineage graph, tables) | UI; data contracts stable in `/api/v1/metrics` and lineage endpoints |
| Auth | None in v1 (localhost); UI assumes trusted local user (ADR 0009) |

## Design language

The visual identity is an **instrument panel**, executed consistently (run-intelligence slice):

- **Display face:** vendored [Departure Mono](../implementation/departure-mono-vendored.md) (SIL OFL) on
  headings, numeric readouts, meters, chart chrome; body prose stays on the vendored terminal-ui monospace stack.
- **Charts are oscilloscope traces:** inline SVG (`<polyline>`/`<rect>`/`<circle>` only — no chart library, no
  runtime fetch) on a dotted graticule, phosphor accent from the active theme's `--fine-use-*` variables, so all
  four vendored themes stay coherent.
- **ASCII meters** (`[###.....] 42%`) for loads, stall ratios, and per-dim sigma — the terminal vocabulary is kept
  where it communicates faster than a graphic.
- **Heat matrix** for cross-episode per-dim variance: CSS-grid intensity cells (`color-mix` on the accent), hover
  for exact values, horizontal scroll with a themed thin scrollbar.
- Escaping is owned by `web/pages.py` (`html.escape` on every dynamic value); themes are the four vendored ones,
  anything else falls back to `vt220`.

## Non-goals (UI)

No dataset editing/curation authoring beyond queries; no labeling tooling (deferred, requirements §7); no real-time
robot teleop; no user management. Framework candidates (React/Vite/TS, Svelte, server-rendered) remain unscored until
phase 12 per [technology-decision-matrix.md](technology-decision-matrix.md) §9.
