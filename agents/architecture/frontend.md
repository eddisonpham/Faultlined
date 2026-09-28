# Frontend — Scope and API Boundary

Visual design happens in phase 12 ([../prompts/12-frontend-design.md](../prompts/12-frontend-design.md)); the
*mechanism* was settled for MVP in [ADR 0014](../decisions/0014-minimal-ui-server-rendered.md): server-rendered
HTML plus vanilla-JS polling, no framework and no build step. Style mandate (spec §4): clean, restrained,
technically credible — a purpose-built engineering tool, not a generic dashboard.

**Implemented in stage 2 so far:** Status/Overview, Jobs (list + detail), Artifacts, at `/ui`, `/ui/jobs`,
`/ui/jobs/{id}`, `/ui/artifacts`. Polling re-requests the same page with `X-Fragment: 1` and swaps the returned
HTML, so Python stays the only renderer. Episodes, Failures, Datasets & Builds, Runs, and Benchmarks remain
stage-3 work.

## Scope

Engineering-facing UI for the personas in [../spec/problem.md](../spec/problem.md) §3. MVP (definition-of-done stage 2)
needs only the **Jobs / Artifacts / Status** slice; the rest lands by Production Baseline (stage 3).

## Information architecture (pages)

| Page | Content | Stage | Primary persona |
|---|---|---|---|
| **Status / Overview** | Health: queue depth, workers, GPU/CPU/disk telemetry, recent failures | MVP | platform |
| **Jobs** | List (filters: type, state, time), detail with state transitions, attempts, logs-by-correlation-id, cancel action | MVP | all |
| **Artifacts** | Browse content-addressed artifacts, sizes, references | MVP | data |
| **Episodes** | Metadata search (FR-005 query builder over indexed fields), detail: stats, validation result + reason codes, quarantine actions | Baseline | data, ML |
| **Failures** | Validation failures grouped by reason code; job failures; revalidate/cancel actions | Baseline | data |
| **Datasets & Builds** | Builds list; build detail: manifest view, **lineage graph** (backwards to episodes, forwards to runs) | Baseline | ML, eval |
| **Runs** | Workload run records: provenance fields, metrics | Baseline | eval |
| **Benchmarks** | Result tables + charts (p50/p95/p99, throughput), baseline diffs, regression flags | Baseline | platform |

## API boundary rules

- The UI consumes **only** `/api/v1` (OpenAPI-generated client; ADR 0009). No direct DB or filesystem access, ever.
- Mutations limited to: submit jobs, cancel jobs, trigger revalidation, trigger builds/runs. Everything else is read-only.
- Live updates in v1: **polling** (jobs list 3 s, detail 5 s, backoff when tab hidden). SSE/WebSocket is a deferred
  trigger (data-flow.md §5).
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

## Non-goals (UI)

No dataset editing/curation authoring beyond queries; no labeling tooling (deferred, requirements §7); no real-time
robot teleop; no user management. Framework candidates (React/Vite/TS, Svelte, server-rendered) remain unscored until
phase 12 per [technology-decision-matrix.md](technology-decision-matrix.md) §9.
