# Current capabilities — what is actually shipped and verified

- **Date:** 2026-10-06 · **Tree:** `693edb3` · Companion to [architecture.md](architecture.md).
- Every row distinguishes **shipped** (code + tests green), **measured** (a number exists in an experiment
  record), and **unmeasured** (asserted or designed, no evidence yet). Nothing below is aspirational.

## 1. Verification baseline

| Gate | State |
|---|---|
| `just ci` (lint, format, mypy strict, tests, hygiene, OpenAPI drift) | green at `693edb3` — 1,399 tests, 89.99 % coverage (floor 70 %), mypy 81 + 22 files |
| OpenAPI contract ([docs/api/openapi.json](../../../docs/api/openapi.json)) | committed copy is the gate; drift fails CI |
| UI audit (`scripts/ui_audit.mjs`, real Chrome) | 56 page/theme loads clean (+4 discovered entry-detail loads) |
| Hosted CI | none — `just ci` run locally is the whole guarantee |

## 2. Capability inventory

### Ingest (shipped + measured)

| Capability | State | Evidence |
|---|---|---|
| Synthetic episode ingest (JSON payload) | shipped, measured | EXP-0001 P50 0.61 ms microbenchmark |
| LeRobot v2.1 / v3.0 dataset ingest | shipped, measured | EXP-0002/0008 (real 203-frame episode), `tests/integration/test_real_dataset_ingest.py` |
| MCAP ingest (JSON channels) | shipped, measured | EXP-0004 6.21 MiB/s P50; EXP-0005 10.8–18.3 MiB/s with shape-compiled flatten; EXP-0010a linear ~24.4 µs/msg, R²≈1.000 |
| Filesystem path ingest (`ingest_source` + `episode_key`) | shipped | `EpisodeIngestService.ingest_path`, reader registry by extension/magic |
| Content-addressed artifact store (SHA-256, atomic publish, verify-on-read) | shipped, measured | EXP-0011 blob integrity verified after worker hard-kill |
| Duplicate-ingest no-op (F26) | shipped | EXP-0011 |
| Crash recovery: reaper + retry convergence | shipped, measured | EXP-0011 (both kill shapes; residue only invisible `.pending-*`) |
| Stage deadline / timeout (F6) | shipped, measured | EXP-0012 (mid-handler interrupt-point re-check) |
| Cancel (ahead-of-worker and mid-flight) | shipped, measured | EXP-0010d cancel effect max 0.175 s ≤ 2 s |
| **Foreign formats** (ROS 2 bag, CDR/protobuf MCAP, UMI HDF5, RLDS, zarr, WebDataset, npz) | **not supported** — refused or silently degenerate | EXP-0014: 19 fixtures → 9 accepted (shape-compatible), 10 cleanly refused, 2 accepted with zero decoded channels and no quality verdict |

### Validation & quality (shipped + measured)

| Capability | State | Evidence |
|---|---|---|
| Rule-based validation profiles, profile-hash pinned | shipped | `validation/rules.py`, `profile.py`; contract tests |
| Bulk validate job over a selection | shipped, measured | EXP-0010c: 86.6 ms/episode at 10k, 10,000/10,000 checked |
| Per-dimension quality analysis + verdicts + traces | shipped, measured | EXP-0002 quality 2.58 ms@303f; EXP-0007 linear at 30k frames |
| Non-finite-value guarantee in `analyze()` | shipped (ADR 0023) | holds in analysis; **broken one layer down** — see [technical-debt.md](technical-debt.md) T-02 |
| Topic-name-independent verdicts | **broken** | EXP-0014 D1 (byte-identical payload judged `smooth` vs `unknown` by topic name) |

### Jobs & pipeline (shipped + measured)

| Capability | State | Evidence |
|---|---|---|
| Postgres job queue (`FOR UPDATE SKIP LOCKED`), idempotency keys, correlation ids | shipped | `jobs/queue.py`, `catalog/repository.py::submit_job` |
| Retry classification (retryable vs terminal, `ReasonCode`s) | shipped | `jobs/worker.py::_settle_failure`, `observability/reason_codes.py` |
| Worker horizontal scaling | measured | EXP-0010b: 2.35× speedup 1→2 workers |
| Multi-process contention behavior | measured | EXP-0016: 3.54 vs 3.58 jobs/s under full load; zero lost writes |
| Orphan/deadline reaping | shipped | `reap_expired_deadlines`, `reap_orphaned_jobs`, run from `_worker_loop` |
| Builds (content-addressed manifest → `build_hash`) | shipped | `builds/service.py`; integration tests |
| Export to LeRobot v3 parquet layout | shipped | `builds/export.py`, `tests/integration/test_build_export.py` |
| Operator-visible failure reason | **broken** | EXP-0014 D3: stored message is the literal `"job handler failed"` |

### Curation & vocabulary (shipped + measured)

| Capability | State | Evidence |
|---|---|---|
| Task-string vocabulary with versioned entries/mappings | shipped | ADR 0029, `catalog/vocabulary.py` (1,075 lines), integration + contract tests |
| Ingest-time task resolution | shipped | `ingest/service.py::_resolve_task` |
| Unmapped queue + deterministic synonym candidates | shipped, measured | `clustering/ranker.py::candidates` (pure, ~0 ms); extraction 48/48 core coverage |
| Accept / map / dismiss / rename / notes / merge / split | shipped | API + UI routes; `accept_candidate` single-transaction |
| Append-only decision events with undo, stale-undo refusal | shipped | `undo_event` + `_require_applicable`; `TestStaleUndo` |
| Per-string write serialization (lost-update fix) | shipped | `_lock_strings` advisory locks, `TestConcurrentWriters` |
| Slices (selections, manifest, impact) | shipped | `curation.py`, repository slice API, contract tests |
| Cluster proposals | **frozen archive** | ADR 0029 supersedes 0026; mutations 410, UI redirects |

### API surface (shipped + measured)

| Capability | State | Evidence |
|---|---|---|
| ~70 JSON endpoints incl. health/status/metrics/contracts/incidents | shipped | OpenAPI contract gate |
| Streamed CSV/JSONL downloads on list surfaces (ADR 0030) | shipped | `api/download.py`, `tests/contract/test_download_api.py` |
| Read latency at 10k scale | measured | EXP-0010c/0010e: all gated endpoints ≤ 300 ms p95 at 10 rps; EXP-0015 full-surface table |
| Read latency at 10k scale for aggregate endpoints | measured over budget | `quality/summary` 399 ms / 2.2 MiB (EXP-0015) |
| Adversarial contract coverage (correlation ids, idempotency, payloads) | shipped | `tests/contract/test_api_adversarial.py` |

### UI (shipped + measured)

| Capability | State | Evidence |
|---|---|---|
| 20+ server-rendered pages, 4 themes, zero JS framework (ADR 0014) | shipped | ui-audit 60 loads clean |
| Ingest + cancel from browser | shipped | `POST /ui/jobs`, `POST /ui/jobs/{id}/cancel` |
| Vocabulary triage from browser (accept/map/dismiss/undo/merge/split) | shipped, measured | EXP-0017: 28 buttons / 27 forms on `/ui/vocabulary`; 10 strings ≈ 6 s waiting |
| Navigation depth | measured | EXP-0017: every journey ≤ 1 click from home |
| **Validate / build / export / slice creation from browser** | **missing** | EXP-0017: `ui_submit_job` builds only `ingest`/`ingest_source` |
| Per-action feedback quality | partially broken | EXP-0014 D3 (error strings), stale-undo refusals do surface reasons |

### Monitoring & observability (shipped, partially measured)

| Capability | State | Evidence |
|---|---|---|
| Structured JSON logs with correlation id end-to-end | shipped | `observability/logging.py`, adversarial contract tests |
| JSONL runtime metric sink + host gauges | shipped | `RuntimeMetrics`, `sample_host` from worker loop |
| Metrics aggregation + `/api/v1/metrics` + `/ui/metrics` | shipped, measured | EXP-0007 tail-read; EXP-0010e N+1 fixed (p95 591→224 ms) |
| Deterministic monitor: features → baselines → detectors → incidents + notify classification | shipped | ADR 0020, `monitoring/`; EXP-0003 evaluate P50 0.2 ms |
| **Monitor execution in a running deployment** | **never happens** | `tick()` callers = its own route + tests (EXP-0016, grep-verified) |
| Detection accuracy (precision/recall) | **unmeasured** | needs the full chaos campaign (B-016); no claim exists |

### Platform / ops

| Capability | State | Evidence |
|---|---|---|
| Versioned catalog migrations (ADR 0028) | shipped | `de migrate [--status]`, `tests/integration/test_migrations.py` |
| `de doctor`, `just pg-up/down/status`, `just reset` | shipped | scripts + CLI |
| Backup/restore + worker-ops runbooks | shipped (manual) | [docs/runbooks/](../../../docs/runbooks/) |
| Garbage collection | **stub** | `cli.py` gc branch prints "not implemented" |
| Authn/authz | **absent** | no auth code anywhere; default bind 127.0.0.1 |
| Workload SDK (models as workloads) | shipped (protocol-level) | `workloads/sdk.py`; `benchmarks/` harness runs workloads with provenance |

## 3. What is deliberately out of scope

Per CLAUDE.md scope guard and ADR 0020: quantization / TensorRT / ONNX / edge optimization is not the
contribution; no model sits in any decision path; no speculative dependencies (ADR 0014 stdlib-only UI).

## 4. Summary judgment

Everything promised for the **core loop** (ingest → validate → curate → build → export) is shipped and,
for the parts reachable from the browser, measured. The measured weak spots are concentrated: three
correctness defects at the ingest boundary (T-01..T-03 in [technical-debt.md](technical-debt.md)), a
monitor that never runs, an API-only downstream pipeline, and O(table) read paths on three endpoints.
