# Architecture — end-to-end map, usage trajectories, component performance

- **Date:** 2026-10-06
- **Tree:** `693edb3` (clean)
- **Scope:** planning-phase map of the whole program, traced from its entry points. Evidence for every
  performance claim comes from the project's own observability stack (structured logs with correlation ids,
  the JSONL runtime metric sink, `/api/v1/metrics`, benchmark provenance) plus the experiment records
  [EXP-0014](../../experiments/0014-foreign-data-ingest-corpus.md),
  [EXP-0015](../../experiments/0015-feature-latency-at-three-scales.md),
  [EXP-0016](../../experiments/0016-concurrent-workflow-contention.md),
  [EXP-0017](../../experiments/0017-operator-click-and-attention-budget.md).
- Companions: [current-capabilities.md](current-capabilities.md), [technical-debt.md](technical-debt.md),
  [missing-production-capabilities.md](missing-production-capabilities.md), [proposed-v2.md](proposed-v2.md),
  [improvement-ranking.md](improvement-ranking.md).

## 1. Entry points

There is exactly one application entry point, `de` ([src/data_engine/cli.py](../../../src/data_engine/cli.py)),
exposed through `just` ([justfile](../../../justfile)):

| Command | What it does | Notes |
|---|---|---|
| `de api` | `initialize_schema` → `uvicorn.run(create_app(...))` | HTTP + server-rendered UI in one process |
| `de worker` | `initialize_schema` → `IngestWorker` loop | `--once` processes a single job |
| `de dev` | worker as a child process + uvicorn | the documented local mode (`just run`) |
| `de doctor` | connectivity + schema check | prints OK or raises |
| `de migrate [--status]` | versioned catalog migrations (ADR 0028) | `--status` exits nonzero if the DB is newer than the code |
| `de gc` | **stub** — prints "No garbage-collection work is implemented" | `cli.py`, `gc` branch |

Operational scripts live in [scripts/](../../../scripts/): `check_port.py` / `stop_server.py` (dev-server
lifecycle), `dev_postgres.py` (`just pg-up`, isolated Postgres on 127.0.0.1:55432), `reset_data.py`,
`openapi_contract.py` (drift gate), `ui_audit.mjs` (browser measurement), and the measurement campaign
scripts (`feature_latency.py`, `multi_workflow.py`, `operator_walkthrough.py`, `foreign_data_corpus.py`,
`foreign_data_probe.py`, `scale_campaign.py`, `fault_drill.py`, `profile_ingest.py`, `make_mcap_log.py`).

`just ci` (lint → typecheck → test → hygiene → api-contract) is the only gate: there is no hosted CI
runner ([.github](../../../.github) contains only `pull_request_template.md`; the justfile says so explicitly).

## 2. Request-level architecture

[api/app.py](../../../src/data_engine/api/app.py) (`create_app`, 1,951 lines) is a single FastAPI application
serving three surface families:

1. **JSON API** `/api/v1/*` — jobs, episodes, artifacts, builds, slices, quality, failures, vocabulary,
   clusters (read-only archive), incidents, monitoring, contracts, status, metrics, streamed downloads.
2. **Server-rendered UI** `/ui/*` (ADR 0014: no framework, no build step; vendored CSS/fonts;
   [web/pages.py](../../../src/data_engine/web/pages.py) renders every page; `app.js` only polls and themes).
3. **Static assets** `/ui/*.css`, `/ui/app.js`, vendored fonts.

Cross-cutting middleware and handlers ([api/errors.py](../../../src/data_engine/api/errors.py)):
`correlation_middleware` assigns/echoes `X-Correlation-Id` (sanitized by `safe_correlation_id`), and
domain exceptions map to RFC-7807-ish problem JSON: `IdempotencyConflict` → 409 `IDEMPOTENCY_KEY_CONFLICT`,
`SliceNameConflict` → 409, `LabelConflict` → 409 `VOCABULARY_LABEL_CONFLICT`, `InvalidTransition` → 409.
Request latency is recorded per route template via `RuntimeMetrics.api_request`.

## 3. The pipeline (data flow)

```
POST /api/v1/jobs (or POST /ui/jobs form)
        │  submit_job() → Postgres jobs table  [idempotency key, correlation id]
        ▼
IngestWorker.claim_job()  (FOR UPDATE SKIP LOCKED)          jobs/queue.py, catalog/repository.py
        │
        ▼
IngestWorker._run_handler()                                 jobs/worker.py
   ├─ ingest          payload carries a synthetic episode dict
   ├─ ingest_source   payload carries a filesystem path + optional episode_key
   │      └─ EpisodeIngestService.ingest_path → reader registry
   │             readers/registry.py resolves by extension/magic:
   │             lerobot.py (LeRobot v2.1 / v3.0 parquet+json), mcap_reader.py (MCAP; JSON and
   │             protobuf/CDR message bytes), plus synthetic dicts
   │      └─ analysis/quality.analyze() → per-dimension quality verdict + trace
   │      └─ storage/artifacts.py FileArtifactStore.put_file → content-addressed blob (SHA-256)
   │      └─ catalog.register_episode (+ episode_quality, vocabulary task resolution ADR 0029)
   ├─ validate        validation/service.py + validation/rules.py profiles (profile_hash pinned)
   ├─ build           builds/service.py → manifest → build_hash (content-addressed dataset build)
   └─ export          builds/export.py → LeRobot v3 parquet layout under settings.export_root
```

Failure handling is inside `process_one` / `_settle_failure` (retryable vs terminal, `ReasonCode`
mapping from [observability/reason_codes.py](../../../src/data_engine/observability/reason_codes.py)),
with deadline enforcement (`_respect_deadline`, `_checkpoint`, `_JobTimedOut`), cancel polling,
and a reaper (`reap_expired_deadlines`, `reap_orphaned_jobs`) run on an interval from `_worker_loop`.
The worker loop survives handler and database exceptions (backoff `DB_ERROR_BACKOFF_SECONDS`) and exits
if its multiprocessing parent dies (`_parent_alive`).

## 4. Persistence

| Store | What lives there | Mechanics |
|---|---|---|
| **PostgreSQL catalog** ([catalog/database.py](../../../src/data_engine/catalog/database.py)) | jobs, episodes, episode_quality, artifacts index, validation profiles/results, builds, slices, vocabulary (entries/mappings/events), clusters (frozen archive), incidents, baselines, contracts | schema DDL applied idempotently at startup (`initialize_schema`) plus versioned migrations (`catalog/migrations/`, ADR 0028); `PostgresCatalog` ([catalog/repository.py](../../../src/data_engine/catalog/repository.py), ~1,900 lines) is the only SQL surface |
| **File artifact store** ([storage/artifacts.py](../../../src/data_engine/storage/artifacts.py)) | episode blobs (JSON/parquet/MCAP-derived) | content-addressed `root/<2>/<hash>`, SHA-256 verified on read, atomic `.pending-*` publish |
| **Export root** | LeRobot v3 datasets | written by `BuildExporter._publish` |
| **Runtime metrics** (`settings.metrics_path`, default `var/metrics/runtime.jsonl`) | every `RuntimeMetrics` signal | append-only JSONL, gitignored; read back by [observability/aggregate.py](../../../src/data_engine/observability/aggregate.py) |

Concurrency discipline: job claiming is `FOR UPDATE SKIP LOCKED`; vocabulary writes are serialized per
task string with transaction-scoped `pg_advisory_xact_lock` in sorted order
([catalog/vocabulary.py](../../../src/data_engine/catalog/vocabulary.py) `_lock_strings`, ADR 0029 amendment);
stale vocabulary undos are refused rather than replayed (`_require_applicable`).

## 5. Vocabulary subsystem (the curation spine, ADR 0029)

Task strings from ingested episodes resolve against a versioned vocabulary:
`_resolve_task` at ingest time (ingest/service.py) → `list_unmapped` queue → `candidates()` ranker
([clustering/ranker.py](../../../src/data_engine/clustering/ranker.py), pure, core-equality only) →
operator decisions via `accept_candidate` (one transaction: revalidate + entry + mappings + events),
`map_task`, `dismiss_task`, `merge_entry`, `split_entry`, `undo_event`. Every decision is an append-only
event row; undo compensates and refuses supersession. The older cluster-proposal surface
([catalog/clusters.py](../../../src/data_engine/catalog/clusters.py), clustering/extract|layout|online|
proposals) is **frozen**: reads answer, mutations return 410, `/ui/clusters*` redirects to the vocabulary
pages (ADR 0029 supersedes ADR 0026).

## 6. Monitoring subsystem (ADR 0020)

[monitoring/service.py](../../../src/data_engine/monitoring/service.py): `MonitorService.tick()` builds
features from the metrics sink + catalog snapshot (`monitoring/features.py`, `signals.py`), compares to
baselines (`baselines.py`, persisted), runs deterministic detectors (`detectors.py`), and upserts incidents
with deterministic notification classification (notify / queue / suppress). Self-observability signals
(`monitor_tick_seconds`, `monitor_signals_total`, `monitor_blind`, …) are emitted per tick.

**Critical structural fact:** `tick()` has exactly two callers — the `POST /api/v1/monitoring/tick` route
([api/app.py](../../../src/data_engine/api/app.py) `run_monitor_tick`) and the tests. Nothing schedules it
in a running deployment (confirmed by grep over `src/` and `tests/`, EXP-0016), so `/ui/incidents` renders
zero rows until someone POSTs a tick by hand.

## 7. Observability (the meta-tracking layer used for this audit)

- **Logs:** `observability/logging.py` — one JSON object per line (`timestamp`, `level`, `service`,
  `event`, `message`, `correlation_id`, optional `job_id`/`episode_id`/`error_type`/`reason_code`).
- **Metrics:** `observability/metrics.py` — `RuntimeMetrics` emits the registry's signals to a JSONL sink;
  never fails the caller (ADR 0008). Host gauges (`system_*`, `process_rss_bytes`) sample on
  `HOST_SAMPLE_INTERVAL_SECONDS` from the worker loop.
- **Aggregation:** `observability/aggregate.py` tail-reads the sink (1 MiB chunks, EXP-0007) and serves
  `/api/v1/metrics` and `/ui/metrics`; cardinality is capped by ADR 0017 (no per-job series).
- **Correlation:** the middleware's id appears in logs, metric points, and responses.
- **Meta-tracking:** benchmark runs write versioned provenance JSON into `benchmarks/results/`
  (`benchmarks/provenance.py`), and every campaign becomes an experiment record in
  [agents/experiments/](../../experiments/README.md) — that registry is how this audit checks claims
  against measurements.

## 8. Frontend/backend coupling

Server-rendered HTML from `web/pages.py` (2,586 lines) plus small page modules (`cluster_page.py`,
`schema.py`, `lineage.py`, `records.py`, `charts.py`). Mutations are plain HTML forms POSTed to `/ui/*`
routes that validate through the **same Pydantic schemas** as the JSON API and answer 303 redirects
(`ui_submit_job` documents this "second front door must not be weaker" rule). The UI reads JSON endpoints
only through `app.js` polling. There is no client-side state store, no SPA router; the coupling surface is
the HTML contract, guarded by `tests/contract/test_ui_polling.py` and `scripts/ui_audit.mjs`
(56 page/theme loads clean). The UI deliberately covers only part of the API — see
[improvement-ranking.md](improvement-ranking.md) item 2.

## 9. Configuration and secrets

[config.py](../../../src/data_engine/config.py): one `Settings` (pydantic-settings, `DE_` prefix) loaded from
the process environment only (ADR 0002) — `database_url` (SecretStr), `artifact_root`, `export_root`,
`log_level`, `log_format`, `worker_slots`, `api_host` (default `127.0.0.1`), `api_port`, `metrics_path`,
and `HF_KEY` (fixed name, never logged). `.env` is read by `just` only, never by the application.

## 10. Deployment

Local-first by design: `just run` on a workstation against a local or `just pg-up` Postgres. Two runbooks
([docs/runbooks/backup-and-restore.md](../../../docs/runbooks/backup-and-restore.md),
[worker-operations.md](../../../docs/runbooks/worker-operations.md)). No container image, no orchestrator
manifests, no hosted CI. The Windows portability traps are encoded in the justfile (`windows-shell`,
script-not-recipe rules). See [missing-production-capabilities.md](missing-production-capabilities.md)
for what this rules out.

## 11. Usage trajectories (simulated)

Each trajectory lists clicks/requests as a real user would take them, with the components it touches and
the measured cost where one exists.

### T1 — First run (empty machine → first episode visible)
1. `just pg-up` (or point `DE_DATABASE_URL` at an existing Postgres) → `just doctor` (prints OK).
2. `just run` → `de dev`: schema initialized, worker child + uvicorn on 127.0.0.1:8000.
3. Open `/ui` (1 click to anywhere — EXP-0017 measured every journey at 1 navigation click).
4. Status page form → `POST /ui/jobs` (synthetic episode) → 303 to `/ui/jobs/{id}`; poll until done.
5. `/ui/episodes/{id}` shows quality verdict + validation.
Cost: 3–4 interactions, no CLI knowledge needed past `just run`. This trajectory works end to end today.

### T2 — Bulk ingest from disk (the robot-lab reality)
1. `POST /api/v1/jobs` `ingest_source` with `{"source": "D:/bags/run-042"}` (or the status page's
   path form, one job per path — there is **no directory scan or batch form**; see
   [technical-debt.md](technical-debt.md) T-08).
2. Worker resolves readers by extension (registry), extracts, analyzes quality, writes artifact blob,
   registers episode, resolves the task string against the vocabulary (ADR 0029) — a new string lands in
   the unmapped queue.
3. `/ui/jobs/{id}` → report (episodes produced, reason codes).
Foreign formats behave per [EXP-0014](../../experiments/0014-foreign-data-ingest-corpus.md): 9/19 fixture
shapes accepted, 10 refused cleanly, 2 (CDR/protobuf MCAP) accepted with zero decoded channels and no
quality verdict.

### T3 — Curation (the workflow the product is betting on, ADR 0029)
1. `/ui/vocabulary` (p95 595 ms at 10k episodes, EXP-0015) → unmapped queue + candidate pairs.
2. Per string: accept candidate / map to entry / dismiss — each is one form POST + full page reload
   (27 forms on the page, EXP-0017); undo is available per event and refuses stale replay.
3. Entry pages allow rename/notes/merge/split.
Measured cost: 10 strings ≈ 10 clicks ≈ 6 s of page-load waiting, plus per-page redundant query work
(182 ms of the 595 ms is `vocabulary_health` re-doing queries the page already ran — EXP-0015).

### T4 — Dataset construction (API-only today)
1. `POST /api/v1/jobs` `validate` with a profile → job report with pass/fail + reason codes
   (10k episodes = 86.6 ms/episode single-worker, EXP-0010c).
2. `POST /api/v1/jobs` `build` over a selection → content-addressed build (`build_hash`).
3. `POST /api/v1/jobs` `export` → LeRobot v3 layout under `export_root`.
4. Browse `/ui/builds/{hash}` and `/ui/slices`.
**There is no browser route for any of step 1–3** (EXP-0017, verified against `ui_submit_job`) — the
stated output of the product is reachable only from the JSON API.

### T5 — Failure handling
Job fails → `process_one` settles it with a `ReasonCode`, retry policy applies; the UI's `/ui/failures`
and `/api/v1/failures` summarize reason codes and failing episodes; logs carry `job_id` + `error_type` +
`correlation_id`. The operator-visible message for an unexpected handler exception is currently the
literal `"job handler failed"` (EXP-0014 D3) — the trajectory *exists* but its information content is
broken at the last step.

### T6 — Concurrent operators / processes (EXP-0016)
Two workers + API readers + vocabulary writers + monitor ticks on one catalog: ingest throughput
3.54 jobs/s alone vs 3.58 jobs/s with everything running; zero lost writes, zero errors; advisory-lock
ordering prevents vocabulary write races. Contention is measured **not** to be a problem at this scale.

### T7 — Monitoring (designed, not scheduled)
`POST /api/v1/monitoring/tick` → 407 ms p50 (EXP-0016: ~0.2 ms of evaluation, the rest is feature
building + sink read) → incidents upsert → `/ui/incidents`. Trajectory T7 cannot occur spontaneously:
see §6.

## 12. Component performance, as seen through the project's own telemetry

p95 unless noted; full tables in [EXP-0015](../../experiments/0015-feature-latency-at-three-scales.md)
(20 trials at 20 / 1,000 / 10,000 episodes, live uvicorn + real Postgres).

| Component | Evidence source | Observed cost @10k | Verdict |
|---|---|---|---|
| HTTP framework floor | EXP-0015 (`monitoring/health`) | 26 ms | healthy; floor is framework overhead |
| Catalog reads (`list_episodes`, `jobs`, `builds`) | EXP-0015 + EXP-0010c | 66–175 ms | healthy, flat against scale |
| Deep pagination | EXP-0015 (`episodes?offset=5000`) | 93 ms | healthy |
| Vocabulary reads | EXP-0015 | 79–213 ms | healthy |
| **`quality/summary`** | EXP-0015 | **399 ms, 2.2 MiB JSON** | over budget; whole-table aggregate per request |
| **`/ui/vocabulary`** | EXP-0015 + EXP-0017 | **595 ms**; 182 ms of it duplicate queries (`vocabulary_health` vs `list_unmapped`+`list_entries`) | over budget on the product's core page |
| **`/ui/insights`** | EXP-0015 | 389 ms, **5.2 MiB HTML** | over budget; unpaginated |
| `/ui/schema` | EXP-0015 | 538 ms | over budget; introspection per request |
| `POST /vocabulary/mappings` | EXP-0015 | 248 ms | the write that runs the lock + events + undo bookkeeping; acceptable but the slowest mutation |
| Worker ingest throughput | EXP-0004/0005/0010a | ~24.4 µs/msg, linear (R²≈1.000); 6.2 MiB/s JSON path, 10.8–18.3 MiB/s flattened | linear and predictable; the format cost dominates |
| Validate throughput | EXP-0010c | 86.6 ms/episode | linear |
| Monitor tick | EXP-0016 | 349–407 ms p50 | cheap enough to run on a 30 s interval — but unscheduled |
| Multi-process contention | EXP-0016 | ±1 % ingest throughput under full load | not a problem |

## 13. What the map shows

The architecture is coherent: one SQL surface, one content-addressed store, one job pipeline with real
crash semantics (EXP-0011), a curation spine with honest undo semantics, and observability good enough to
audit the product with its own instruments. The weak points are all at the edges of that core: the browser
covers half the pipeline, the monitor is never invoked, several read paths are O(table) per request, and
the ingest boundary to non-LeRobot data is narrower than the marketing shape of the product implies.
Those are ranked with citations in [improvement-ranking.md](improvement-ranking.md).
