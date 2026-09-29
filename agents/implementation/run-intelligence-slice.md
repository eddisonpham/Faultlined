# Slice: Run Intelligence — monitor, analyze, inspect robot data runs

**Status: packages A–D landed 2026-09-29 (`b4d70a1`, `499249b`, `419a4ca`, `b0052a3`); run-inspection
extension (package F below) lands on top.** The validation stage is closed (commit `6f16adb`).
This slice turns Faultlined from a pipeline that *moves* episodes into one that *understands* them: expanded
runtime monitoring, robotics-native quality analytics, and an interactive instrument UI to inspect how runs
perform. Every feature is latency-tested and recorded under [../experiments/](../experiments/).

## Why these features (production-system research)

Source log #46–#47. The strongest signal came from the closest production system,
[huggingface/lerobot-dataset-visualizer](https://github.com/huggingface/lerobot-dataset-visualizer) (cloned,
read, deleted): its **Filtering Panel** flags low-movement / jerky / outlier-length episodes for removal from
training sets, and its **Action Insights Panel** computes autocorrelation (suggested action chunk), speed
distribution, and cross-episode variance. Foxglove (source-log, earlier phases) contributes the linked
inspection idea: synchronized data views over one episode. These are the features a robotics ML engineer
actually reaches for when curating training data — and episode-quality-driven *curation* is precisely the
platform's thesis ("the platform is the product"), so we implement them as first-class engine services, not
viewer decorations.

**Design source.** The visual identity is anchored on a real, attributable asset:
[Departure Mono](https://departuremono.com/) (Helena Zhang, SIL OFL 1.1, `rektdeckard/departure-mono`) — a
pixel monospace with a lo-fi technical vibe, sized for crisp rendering at 11 px. It is vendored as woff2 with
its OFL text and a `NOTICE.md`, mirroring the `terminal-ui-vendored.md` precedent. Visual language:
oscilloscope/instrument readout — phosphor trace colors, dotted graticule chart backgrounds, segmented
numeric readouts — executed consistently on top of the already-vendored terminal UI CSS.

## Work packages

### A. Monitoring: runtime metrics expansion (ADR 0017)

- `data_engine/observability/aggregate.py`: summarize the JSONL metrics sink into per-metric summaries
  (count, sum, mean, p50/p95/p99, rate) + time-bucketed series for sparklines. Pure functions over records;
  the sink stays the source of truth.
- **API latency middleware** (`api_request_duration_seconds`, `api_requests_total`, labels: route template,
  method, status class — never raw paths).
- **Catalog query instrumentation** (`catalog_query_duration_seconds`, label: operation) in the repository,
  so slow SQL is visible next to slow handlers.
- **Worker heartbeat**: the worker emits `workers_heartbeat_age_seconds`-compatible heartbeat records
  (`worker_heartbeat` events) at claim/settle time; aggregation exposes the latest age. (Full lease-based
  heartbeats remain deferred per ADR 0015; this gives dead-worker detection in the UI without schema work.)
- `GET /api/v1/metrics`: aggregated metrics + live job-state counts (JSON, contract-tested). Optional
  `?window_seconds=` for the series.

### B. Analysis: episode quality signals at ingest (ADR 0018)

Formulas borrowed from the visualizer (exact source-log citations), computed once at ingest while frames are
already in memory (no byte re-read, matching the validation philosophy) and persisted per episode:

| Signal | Definition |
|---|---|
| `movement_score` | mean L2 norm of frame-to-frame deltas across dims (units/s via fps) |
| `jerk_score` | mean abs frame-to-frame delta normalized by per-dim motor range, active dims only |
| `stall_ratio` | fraction of frames where every dim's abs delta < 0.1% of its motor range |
| `length_zscore` | episode length vs dataset mean/σ at ingest time |
| per-dim | activity flag (p95(|Δa|) ≥ 0.1% range), discrete flag (≤ 4 unique values), normalized σ of deltas |
| `verdict` | `smooth` / `moderate` / `jerky` from absolute bands on normalized σ (<0.02 / <0.1 / ≥0.1) over judged dims; discrete dims not judged, gripper dims excluded. Deviation from source-log #46: the visualizer's bands are relative to the roughest dim, which is degenerate (the max dim always lands in its "jerky" bucket); recorded in ADR 0018 |

- Persistence: `episode_quality` table (episode FK, scalars + per-dim JSON) via idempotent migration;
  `record_episode_quality`, `get_episode_quality`, `quality_summary` in the catalog repository.
- Analysis module `src/data_engine/analysis/quality.py` is pure (arrays in, `EpisodeQuality` out) so it is
  unit-testable without Postgres.
- API: `GET /api/v1/episodes/{id}/quality`; `GET /api/v1/quality/summary` (length histogram, speed
  distribution, cross-episode per-channel variance matrix, top jerky / stalled / outlier-length episodes).

### C. UI: interactive inspection (ADR 0014 stays: server-rendered + vanilla JS polling)

- `/ui/metrics` — live telemetry dashboard: sparkline series (inline SVG updated by polling), job-state
  meters, stage-latency bars, API-route latency table with p50/p95/p99.
- `/ui/episodes` — episode index: state, verdict badge, movement/jerk/stall columns, query-param filters
  (`?flag=jerky|stalled|short|long`, `?state=`).
- `/ui/episodes/{id}` — inspector: metadata, channel stats table, quality panel (per-dim activity bars,
  verdict readout), quality signal meters.
- `/ui/insights` — dataset view: episode-length histogram, speed-distribution strip plot, cross-episode
  variance heatmap (CSS grid), outlier lists linking to inspectors.
- Design revamp: `@font-face` for vendored Departure Mono (display + readouts), graticule chart backgrounds,
  phosphor trace palette per theme, segmented readout styling in `faultlined.css`. Charts are hand-built SVG
  (no chart library — ADR 0014).

### D. Latency & performance testing (methodology-compliant)

New/updated workloads in `benchmarks/` + experiment records (all raw samples + provenance; **no new
committed baselines without owner authorization**):

| ID | Workload | Question |
|---|---|---|
| B-003 | LeRobot ingest (real fixtures) | episodes/s + latency percentiles for v2.1 and v3.0 paths |
| B-012 | Validation evaluation | latency of the 7-rule profile over a stored episode |
| B-013 | Quality analysis | latency of signal computation vs frame count (100–3000 frames) |
| B-014 | API/UI latency | p50/p95/p99 per endpoint incl. `/api/v1/metrics`, `/quality/summary`, UI TTFB |

Plus micro-benchmarks for `aggregate.py` over large JSONL inputs (regression tripwire, EXP-0001 style) and
unit-level performance smoke bounds where they cannot flake (generous, marked `slow`).

### E. Documentation & hygiene

Same-commit doc updates: `architecture/{api,components,frontend,data-flow}.md`, `observability/conventions.md`
(new metric rows), `testing/failure-modes.md`, `implementation/status.md`, `HANDOFF.md`, benchmark backlog
(rows B-012–B-014), experiment records with provenance, vendored-asset notes (`departure-mono-vendored.md`),
source-log #46–#47. Conventional commits, one work package each, `just ci` green before every commit and
push.

### F. Run inspection surfaces (owner steer: breadth of useful features over polish)

Added after packages A–D, per the owner's direction: since Faultlined is used by developers and ML robotics
engineers, prioritize a **wide range of useful features to monitor, analyze, and inspect robotics runs** over
UI neatness. These extend the API and UI with run-inspection queries over data the catalog already holds:

- `GET /api/v1/episodes` — episode catalog with `?state=` and `?flag=jerky|stalled|short|long` quality
  filters (`EpisodeSummary` carries quality columns so a list is one round trip).
- `GET /api/v1/episodes/{id}/validation` — per-profile verdicts, reason codes, and violations for an episode
  (`ViolationCode` values, not rule names), the "why was this quarantined" answer.
- `GET /api/v1/jobs/{id}/episodes` — reverse lineage: episodes produced by a job (`episodes_produced_by`),
  closing the run → output inspection loop.
- `GET /api/v1/jobs/{id}/report` — run triage card: what the run produced (state/verdict/flag counts, length
  summary, mean movement/jerk/stall), how validation judged it (passed/failed, reason-code frequencies),
  shown on the job page as a Run report section. Latency-tested as `api-job-report`
  (p50 6.7 ms / p95 9.5 ms).
- UI: the episode inspector gains a validation section (per-profile verdict badges, reason codes, violation
  list); the job detail page gains a produced-episodes section.
- Latency-tested alongside the rest: `api-episodes-catalog` (p50 6.9 ms / p95 8.5 ms) and
  `api-episode-validation` (p50 5.7 ms / p95 7.0 ms), isolated runs per [ADR 0019](../decisions/0019-benchmark-workload-isolation.md).

## Sequencing

1. Plan + research docs (this file, backlog, source log) → commit.
2. A: metrics expansion + `/api/v1/metrics` + tests → commit.
3. B: quality analysis + persistence + API + tests → commit.
4. C: UI pages + design revamp + font vendoring + tests → commit.
5. D: benchmark workloads + runs + experiment records → commit.
6. F: run inspection endpoints + UI sections + tests + latency measurements → commit.

## Explicitly out of scope

Video decode/playback, 3D/URDF views, distributed tracing (ADR 0008 trigger not met), lease-based heartbeat
schema, ML model workloads (the platform is the product), any committed performance baseline without owner
review.
