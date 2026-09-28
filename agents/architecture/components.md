# Components

One entry per component in [overview.md](overview.md)'s component map. Every component appears in
[repo-layout.md](repo-layout.md), [../implementation/status.md](../implementation/status.md), and the failure table in
[failure-handling.md](failure-handling.md). Owner roles: [../roles/](../roles/).

## 1. API service (`api/`)

- **Responsibility:** HTTP+JSON surface (ADR 0009): resource CRUD, job submission/cancellation, query endpoints,
  health. Auth: localhost-bind, no auth in v1.
- **Inputs:** HTTP requests (OpenAPI-contracted), `Idempotency-Key` headers.
- **Outputs:** JSON resources, problem-object errors, job handles; call-through to catalog and job queue.
- **Dependencies:** catalog, job queue, config, observability. Never runs pipeline stages in-process.
- **Failure behavior:** validation errors → 4xx problem objects; dependency (DB) down → 503 with retry-after;
  request correlation ID always logged. FR-013.
- **Owner:** implementer (contract tests: test-engineer).

## 2. Catalog (`catalog/`)

- **Responsibility:** System of record (ADR 0005): episodes, metadata index, validation results, datasets, builds,
  lineage edges, runs, jobs, artifacts, idempotency keys. Transactional repositories; schema outline in
  [storage.md](storage.md).
- **Inputs:** typed repository calls from all stages and the API.
- **Outputs:** rows/records; lineage queries both directions (FR-008).
- **Dependencies:** PostgreSQL (via connection pool), observability.
- **Failure behavior:** transaction rollback keeps catalog/artifact consistency (commit artifacts first, then rows);
  connection loss → retry with backoff, callers surface job-level failure. FR-004, FR-008.
- **Owner:** implementer.

## 3. Job queue + scheduler (`jobs/`)

- **Responsibility:** Job lifecycle FR-009: enqueue (idempotent), atomic claim (`FOR UPDATE SKIP LOCKED`), priority
  FIFO, bounded retries with backoff + jitter, per-stage timeouts, cooperative cancellation, worker heartbeats and
  stale-claim requeue (NFR-006). State machine in [data-flow.md](data-flow.md).
- **Inputs:** job submissions (API/CLI), worker claims/completions/heartbeats, cancel requests, timeout ticks.
- **Outputs:** job state transitions (all logged with `job_id`), dispatch payloads to workers.
- **Dependencies:** catalog (jobs table), observability.
- **Failure behavior:** duplicate submission absorbed by idempotency keys; dispatcher crash → jobs stay claimable;
  at-least-once stance, handlers idempotent (ADR 0005).
- **Owner:** implementer (failure tests: test-engineer).

## 4. Worker pool (`jobs/worker.py`)

- **Responsibility:** Run stage handlers out-of-process: claim job → execute handler → report result; heartbeat while
  running; honor cancel/timeout signals; process-parallel for CPU stages, GPU serialization per
  [compute-orchestration.md](compute-orchestration.md).
- **Inputs:** claimed job payloads; handler registry.
- **Outputs:** stage results (artifacts + catalog rows), structured logs, per-job metrics.
- **Dependencies:** stage handlers, job queue, artifact store, observability.
- **Failure behavior:** crash mid-job → heartbeat expiry requeues (at-least-once); OOM/disk-full → job fails with
  reason code, artifacts of a failed attempt are garbage-collectable (content-addressed orphans). FR-010.
- **Owner:** implementer.

## 5. Ingest (`ingest/`)

- **Responsibility:** Register raw episodes (FR-001): readers for MCAP and LeRobot v2/v3 dirs; copy/link bytes into
  the artifact store (content hash), create episode records; idempotent by source hash.
- **Inputs:** file paths/URLs + source metadata; ingest job payload.
- **Outputs:** `episode` records (state: `ingested`), artifact entries, per-channel summary for the validator.
- **Dependencies:** artifact store, catalog, readers (`ingest/readers/`), observability.
- **Failure behavior:** corrupt/truncated files rejected with reason codes (`INGEST_*`), never partially registered
  (transaction); duplicates detected by source hash → idempotent success. FR-001.
- **Owner:** implementer.

## 6. Validation (`validation/`)

- **Responsibility:** Rule-based quality gating (FR-002): declarative validation profiles (YAML, versioned, hashed);
  rule registry (schema/key presence, monotonic time, frequency bounds, NaN/latency, duration); stable reason codes;
  quarantine state for failures; re-validation without re-ingest (FR-003).
- **Inputs:** episode records + artifact bytes + profile.
- **Outputs:** `validation_result` rows (pass/fail + `reason_codes[]`), episode state `valid|quarantined`.
- **Dependencies:** catalog, artifact store (read), rule plugins.
- **Failure behavior:** rule exceptions → recorded as `VALIDATION_RULE_ERROR` (fail closed), not swallowed; profile
  errors rejected at submission time. FR-002, FR-003.
- **Owner:** implementer.

## 7. Indexing (`indexing/`)

- **Responsibility:** Extract episode metadata + frame statistics after validation (FR-004): durations, task/robot
  fields, channel stats (rates, gaps, sizes), quality flags; write catalog rows and Parquet metadata exports.
- **Inputs:** validated episode records + bytes.
- **Outputs:** `episode_metadata` rows, Parquet chunks under the artifact store, queryable index.
- **Dependencies:** catalog, artifact store, PyArrow.
- **Failure behavior:** partial index rebuildable by re-running the stage (idempotent upsert keyed by episode +
  extractor version). FR-004.
- **Owner:** implementer.

## 8. Builds (`builds/`)

- **Responsibility:** Curation → dataset builds (FR-005–FR-007): compile metadata queries against the index; plan a
  build (sorted source list); materialize a LeRobot v3 directory; write the build manifest + lineage edges; verify
  rebuild determinism (NFR-004 test).
- **Inputs:** selection query, build config, profile hash reference.
- **Outputs:** `dataset_build` records + manifest artifact + build directory artifact + lineage (`contains`,
  `derived_from` edges).
- **Dependencies:** catalog, artifact store, LeRobot exporter, query engine.
- **Failure behavior:** empty/mismatched selections fail fast with reason codes; materialization is staged
  (temp dir → hash → atomic publish); re-runs dedupe by manifest hash. FR-006, FR-007.
- **Owner:** implementer.

## 9. Workload runner (`workloads/`)

- **Responsibility:** Narrow workload interface (FR-012): registered workload types receive a dataset build + config,
  run (CPU or GPU), emit artifacts + a run record (spec §9 fields). This is the *only* place model code executes.
- **Inputs:** workload spec (type, version, config, dataset build hash, seeds).
- **Outputs:** artifacts, `run` records + metrics rows.
- **Dependencies:** catalog, artifact store, resource slots (GPU admission), observability.
- **Failure behavior:** workload crash/OOM → run record `failed` with reason code; GPU absent → `gpu: optional`
  workloads fall back per their contract, `gpu: required` are rejected at admission. FR-011, FR-012.
- **Owner:** implementer (interface) + benchmark-engineer (reference workloads).

## 10. Artifact store (`storage/`)

- **Responsibility:** Content-addressed immutable file store (ADR 0006): `sha256` keyed blobs + logical build
  directories; dedupe; integrity checks; disk-space accounting.
- **Inputs:** byte streams / staged directories.
- **Outputs:** hashes, artifact entries, streams for readers.
- **Dependencies:** filesystem (layout in [storage.md](storage.md)).
- **Failure behavior:** write failure → job fails cleanly (no catalog row without artifact); checksum verification on
  read; orphan GC for unreferenced hashes. FR-018.
- **Owner:** implementer.

## 11. Observability (`observability/`)

- **Responsibility:** Structured JSON logging with correlation-ID context (FR-014), resource telemetry (NVML +
  CPU/RAM/disk, degrades without GPU — FR-015), metrics recording shared with the benchmark schema (FR-017).
- **Inputs:** log calls, timer/counter calls, periodic sampler ticks.
- **Outputs:** JSON log stream (stdout + file), metrics rows/files per [../benchmarking/metric-schema.md](../benchmarking/metric-schema.md).
- **Dependencies:** stdlib + pynvml (optional at runtime).
- **Failure behavior:** telemetry absence never blocks the pipeline (`gpu_present=false`); log sink failure degrades to
  stderr. ADR 0008.
- **Owner:** observability-engineer.

## 12. CLI (`cli.py`)

- **Responsibility:** Developer/operator entry points over the same services the API uses: `ingest`, `validate`,
  `build`, `run`, `jobs` (list/cancel), `bench`, `doctor` (env check). One command per dev task (NFR-011).
- **Inputs:** argv + env config.
- **Outputs:** console summaries (human) or `--json` (scriptable).
- **Dependencies:** API client or in-process services (documented per command).
- **Failure behavior:** nonzero exit codes by error class; never mutates catalog state outside job semantics.
- **Owner:** implementer.

## 13. Engineering UI (`frontend/`)

- **Responsibility:** Inspect and operate: jobs/queue, episodes search, datasets/builds + lineage, runs, validation
  failures, benchmark results, artifacts ([frontend.md](frontend.md)). Visual design deferred to phase 12.
- **Inputs:** API only (no direct DB/files).
- **Outputs:** operator actions (submit/cancel jobs) via API.
- **Dependencies:** API contract (OpenAPI-generated client).
- **Failure behavior:** API-unavailable banner + retry; never blocks engine operation.
- **Owner:** frontend-engineer.

## 14. Benchmark harness (`benchmarks/`)

- **Responsibility:** Reproducible stage benchmarks (FR-017): fixed workloads, warmup, repeated trials, persisted
  metric-schema results, baselines for regression checks. Methodology: [../benchmarking/methodology.md](../benchmarking/methodology.md).
- **Inputs:** benchmark specs + fixture datasets.
- **Outputs:** result records (metric schema), baseline files, regression verdicts.
- **Dependencies:** all stages (as clients), observability.
- **Failure behavior:** flaky-prone setup is guarded (warmup + retries); regressions fail the bench command, not the
  engine.
- **Owner:** benchmark-engineer.
