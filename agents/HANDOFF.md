# Handoff

**Status: Scaffolding; review verdict is accept-with-follow-ups (2026-09-28).** The project is **Faultlined**. All engineering findings are resolved and gated; the only blocker is publishing a measured benchmark baseline, which requires the owner's explicit green flag. See [engineering review](reviews/2026-09-28-scaffolding.md) and [definition of done](spec/definition-of-done.md). All work is committed on `main` and pushed. Do not tag `scaffold-complete` until the baseline and the residual verifications below are closed.

## 1. Current problem definition

Build a local-first robot episode data engine to ingest, validate, index/version, curate and build reproducible robot datasets (MCAP / LeRobot) with lineage for ML workloads. The platform is the product; models are workloads. The current vertical slice accepts only a small synthetic JSON episode, not real robotics data. See [problem](spec/problem.md) and [requirements](spec/requirements.md).

## 2. Current architecture

Python modular monolith, FastAPI API, PostgreSQL catalog/job table, one separate local worker process, content-addressed local filesystem artifact store, stdlib JSON logging, psutil host snapshots with optional NVML sampling. The phase 05 path is API submit → Postgres queue → worker → synthetic canonical JSON artifact → episode/lineage catalog → API read. Current schema initialization is inline idempotent DDL, not migrations; artifact and DB updates are not a cross-store atomic transaction. Benchmark foundations are separate and use an in-memory catalog. See [architecture overview](architecture/overview.md), [data flow](architecture/data-flow.md), and [vertical slice](implementation/vertical-slice.md).

## 3. Repository structure

- `src/data_engine/`: API, catalog, jobs/worker, synthetic ingest, artifact storage, config, observability, CLI.
- `benchmarks/`: result schema, provenance capture, statistics, synthetic harness; no accepted baseline.
- `tests/`: unit, contract, integration, and E2E tests.
- `agents/`: persistent specifications, architecture, implementation status, test plans, benchmarking, observability, ADRs, reviews, prompts, experiments.
- `.github/workflows/`: CI and hygiene workflows currently duplicate the same quality job.

See [repo layout](architecture/repo-layout.md).

## 4. Technology stack

Python 3.14, uv/uv.lock, just, Ruff, mypy, pytest/pytest-cov, FastAPI/Pydantic, psycopg 3/PostgreSQL 17+, PyArrow and MCAP dependencies (not yet used for real readers), psutil, optional `nvidia-ml-py` for NVML. Host development is the supported local path; Docker is optional. Decisions: [ADRs 0001–0013](decisions/README.md).

## 5. Implementation status — works / stubbed

**Implemented foundations (current checkout):**
- FastAPI POST job submission and GET job/episode/health subset; correlation middleware and typed request/response schema.
- Postgres schema/repository for jobs, artifacts, episodes, and lineage; idempotent submission and atomic queue claim; queue-depth query.
- One ingest worker process; atomic SHA-256 filesystem artifact writes; synthetic JSON canonicalization and lineage readback.
- Unit/contract/E2E tests and Postgres-marked integration tests.
- JSON log formatter, request/job correlation context, psutil/NVML host telemetry with nullable fallbacks.
- **Runtime metrics emitted at call sites**: queue depth, queue/run time, stage duration, failures by reason code, episode and artifact counters (`RuntimeMetrics`, JSONL sink configured by `DE_METRICS_PATH`).
- Benchmark schema v2, deterministic statistics, provenance capture, and a CLI that validates results and baselines, refuses to publish from a failed run, and fails closed with an actionable message.

**Not implemented or incomplete:**
- MCAP/LeRobot readers, real-data ingest, validation rules/quarantine, Parquet indexing, LeRobot v3 builds, workload execution, frontend, job retries/leases/timeouts/cancellation/heartbeats, GC, formal DB migrations, and a committed benchmark baseline.
- `just bench` runs and produces a valid result, then exits 1 because no baseline is committed. That is the intended, awaited state.

See [implementation status](implementation/status.md), [failure modes](testing/failure-modes.md), and [review findings](reviews/2026-09-28-scaffolding.md).

## 6. Known technical risks

1. **No benchmark baseline yet — awaiting the green flag.** This is an authorization gap, not a tooling gap. When authorized, follow the runbook in [§13](#13-baseline-runbook-owner-green-flag-required).
2. **Hardware matching is exact.** A baseline only compares on a byte-identical CPU/RAM/GPU/OS/disk profile; on a different machine the harness refuses to compare. Revisit with evidence before gating CI.
3. **Postgres path is unverified in this environment:** tests skip without `DE_DATABASE_URL`; do not open or recover `.env`. An operator may supply configuration through their environment.
4. **Crash consistency:** artifact publication and DB catalog writes are separate transactions; worker crash recovery, retries, leases, and orphan GC remain future work.
5. **Clean-clone review and history secrets scan are not performed.** Current-tree hygiene passes; no `.env` was opened.

## 7. Highest-priority next steps

1. **Owner green flag → run the baseline runbook in [§13](#13-baseline-runbook-owner-green-flag-required).**
2. Verify the slice against a real PostgreSQL instance (operator-provided `DE_DATABASE_URL`).
3. Validate a clean clone end-to-end and run a history-wide secrets scan; then repeat the review and consider `scaffold-complete`.
4. MVP engineering order: real MCAP/LeRobot reader on a fixed subset → rule-based validation/quarantine → Parquet metadata index → durable job lifecycle/recovery → deterministic LeRobot v3 dataset build.

## 8. Open architectural questions

- What small public MCAP/LeRobot data subset will be versioned and used as the reproducible MVP fixture?
- What exact catalog migrations and transaction/outbox strategy are needed to reconcile artifact writes with database state?
- What retry/lease/cancel semantics and worker concurrency limits will be implemented first?
- Should runtime metrics initially remain JSONL files or move to a database/exporter once actual query needs exist? Follow ADR 0008 triggers; avoid adding a telemetry service speculatively.
- What machine/hardware profile and owner-approved run conditions define the first committed baseline?

## 9. Benchmarking status

Harness and schema v2 are implemented and tested; `synthetic-episode-ingest` measures a tiny synthetic episode using an in-memory catalog and the filesystem artifact store (3 warmups, 10 trials). It is **not** a database, API, MCAP, LeRobot, or production throughput benchmark. A verification run produced a valid schema-v2 result with full provenance, but **no result has been published as a measurement and no baseline is committed**; EXP-0001 stays planned. Backlog: [benchmark backlog](benchmarking/backlog.md); rules: [methodology](benchmarking/methodology.md); review: [scaffolding review](reviews/2026-09-28-scaffolding.md).

## 10. Testing status

Latest local gates: `just fmt && just lint && just typecheck && just test && just hygiene && uv lock --check` passed; **91 passed, 3 skipped, 91.34% coverage**, hygiene clean, lock current. A Starlette/httpx `TestClient` deprecation warning is non-blocking and deferred. The 3 skips are PostgreSQL-dependent (`DE_DATABASE_URL` not configured). No clean-clone run or live DB verification has been completed; repeat all gates after any change.

## 11. Observability status

JSON logs and correlation IDs flow across the current API → persisted job → worker → logs path. Host telemetry snapshots CPU/RAM/disk/network with optional GPU fields; host fields become nullable on psutil/OS errors, with an AccessDenied test in the passing suite. **Runtime metrics are emitted** at the worker and ingest call sites — `jobs_queue_depth`, `jobs_queue_time_seconds`, `jobs_run_time_seconds`, `jobs_failures_total`, `pipeline_stage_duration_seconds`, `episodes_ingested_total`, `artifacts_written_bytes_total` — written to a JSONL sink at `DE_METRICS_PATH` (default `var/metrics/runtime.jsonl`). Labels stay low-cardinality; IDs appear only in logs. Sink failures never break the pipeline. Distributed tracing remains deferred per [ADR 0008](decisions/0008-observability.md); psutil is recorded in [ADR 0013](decisions/0013-cross-platform-resource-telemetry.md).

## 12. Important decisions and rejected alternatives

- Problem: robot episode data engine (ADR 0003); platform over model-specific optimization.
- PostgreSQL catalog + thin custom queue (ADR 0005); content-addressed local artifacts / ecosystem data formats (ADR 0006); build-thin lineage/run records (ADR 0007).
- Stdlib JSON logging, correlation IDs, file metric direction, defer OTel/Prometheus/Grafana until need (ADR 0008); FastAPI (ADR 0009); modular monolith + separate worker (ADR 0010); 70% coverage floor (ADR 0011); host-based development, containers optional (ADR 0012); psutil host telemetry (ADR 0013).
- No performance approach has been measured or adopted; there are no experiment-backed optimization claims. Alternatives and triggers are recorded in the linked ADRs and [experiment registry](experiments/registry.md).

## 13. Baseline runbook (owner green flag required)

Do not start until the owner authorizes a measured baseline. Preconditions: a committed, clean tree; the machine you
will measure on must stay idle; nothing else heavy may run during the measurement.

```bash
just ci                                  # 1. all gates green before measuring
git status --short                       # 2. must be empty (provenance records dirty state)
just bench --write-baseline --baseline benchmarks/baselines/synthetic-ingest-$(git rev-parse --short HEAD).json
just bench                               # 4. re-run: must now compare and report no regression
```

Then, before tagging:

1. Inspect the written baseline and the raw result under `benchmarks/results/`. Confirm provenance is complete:
   git commit, dirty flag, config hash, hardware, OS/Python/lockfile, seed, timestamp, workload version, background-load note.
2. Update [EXP-0001](experiments/0001-synthetic-ingest-baseline.md) with the actual numbers, the hardware profile, and
   honest caveats. Do not restate the number as a throughput or production claim — it is a synthetic local microbenchmark.
3. Update [the experiment registry](experiments/registry.md) to record the measured entry.
4. Tick the Benchmarking criterion in [the definition of done](spec/definition-of-done.md) with links to the baseline,
   EXP-0001, and the result file. Leave Handoff unticked until the clean-clone and secrets-scan items close.
5. Re-run `just ci`, then commit as `bench: record first measured baseline` and push.
6. Only after the Handoff criterion is also evidenced, tag `scaffold-complete`.
