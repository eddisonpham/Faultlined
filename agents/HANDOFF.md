# Handoff

**Status: Scaffolding in progress; stage acceptance rejected on 2026-09-28.** See [engineering review](reviews/2026-09-28-scaffolding.md) and [definition of done](spec/definition-of-done.md). Phase 05/06 implementation remains uncommitted in the current working tree; review before staging or splitting commits. Do not tag `scaffold-complete` until the blocking criteria are closed and the review is repeated.

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
- Postgres schema/repository for jobs, artifacts, episodes, and lineage; idempotent submission and atomic queue claim.
- One ingest worker process; atomic SHA-256 filesystem artifact writes; synthetic JSON canonicalization and lineage readback.
- Unit/contract/E2E tests and Postgres-marked integration tests.
- JSON log formatter, request/job correlation context, psutil/NVML resource snapshot, metric point/JSONL sink primitives.
- Benchmark schema, summary statistics, provenance capture, synthetic local workload and baseline compare/write CLI. Initial uncommitted fixes validate raw-trial consistency and resource references, reject failed results for comparisons/baseline writes, require exact recorded hardware-profile match, and test failure/mismatch paths. They passed local gates; baseline/model-schema validation remains open.

**Not implemented or incomplete:**
- MCAP/LeRobot readers, real-data ingest, validation rules/quarantine, Parquet indexing, LeRobot v3 builds, workload execution, frontend, complete job retries/leases/timeouts/cancellation/heartbeats, GC, formal DB migrations, runtime metric instrumentation, and a committed benchmark baseline.
- `just bench` runs a synthetic workload but cannot complete comparison from a clean checkout because there is no baseline. Warmup failures still abort without a result file; confidence interval consistency and Pydantic/JSON Schema parity remain follow-ups.

See [implementation status](implementation/status.md), [failure modes](testing/failure-modes.md), and [review findings](reviews/2026-09-28-scaffolding.md).

## 6. Known technical risks

1. **Benchmark gate is not ready:** no baseline exists; default `just bench` fails at comparison on a clean checkout. Do not write a baseline until explicitly authorized and after correctness findings are resolved.
2. **Benchmark trustworthiness:** selected raw-trial summaries/status/references and hardware comparison checks were added and locally gated, but are uncommitted. Warmup errors, bootstrap CI consistency, baseline schema validation, and model/JSON Schema parity remain.
3. **Observability gaps:** runtime metrics are not emitted at queue/job/stage call sites. Initial uncommitted host telemetry changes return nullable fields on psutil/OS errors and an AccessDenied test passes; broaden failure-path tests and verify provenance's separate hardware sampling as well.
4. **Postgres path is unverified in the current test environment:** tests skip without `DE_DATABASE_URL`; do not open or recover `.env`. An operator may provide configuration through their environment for later verification.
5. **Crash consistency:** artifact publication and DB catalog writes are separate transactions; worker crash recovery, retries, leases, and orphan GC remain future work.
6. **Clean-clone review and complete secrets audit:** not performed. No `.env` was opened. The duplicate GitHub quality workflows should be consolidated or differentiated.
7. Mixed phase 05/06 uncommitted work must be reviewed and carefully separated before any commit; avoid staging unrelated pre-existing changes.

## 7. Highest-priority next steps

1. Resolve benchmark follow-ups: CI consistency, warmup failure persistence, baseline schema validation, and Pydantic/JSON Schema parity.
2. Wire runtime metrics or explicitly defer specific registry entries; broaden telemetry and provenance fallback tests.
3. Ask the owner to authorize a controlled benchmark measurement. If authorized, preserve provenance, write the baseline, update EXP-0001 with real numbers/caveats, and verify default `just bench`.
4. Run PostgreSQL-marked integration/E2E tests using operator-provided `DE_DATABASE_URL`; do not inspect local secret files.
5. Reconcile duplicate CI, conduct clean-clone documented-command validation and approved history secret scan, repeat the review, then stage/commit phase 05/06 logical pieces. Tag only after explicit stage acceptance criteria are met.
6. MVP engineering order: real MCAP/LeRobot reader on a fixed subset → rule-based validation/quarantine → Parquet metadata index → durable job lifecycle/recovery → deterministic LeRobot v3 dataset build.

## 8. Open architectural questions

- What small public MCAP/LeRobot data subset will be versioned and used as the reproducible MVP fixture?
- What exact catalog migrations and transaction/outbox strategy are needed to reconcile artifact writes with database state?
- What retry/lease/cancel semantics and worker concurrency limits will be implemented first?
- Should runtime metrics initially remain JSONL files or move to a database/exporter once actual query needs exist? Follow ADR 0008 triggers; avoid adding a telemetry service speculatively.
- What machine/hardware profile and owner-approved run conditions define the first committed baseline?

## 9. Benchmarking status

Harness and schema foundations exist; `synthetic-episode-ingest` measures a tiny synthetic episode using an in-memory catalog and the filesystem artifact store (3 warmups, 10 trials). It is not a database, API, MCAP, LeRobot, or production throughput benchmark. **No real result, experiment outcome, or baseline has been published.** EXP-0001 is planned only. Backlog: [benchmark backlog](benchmarking/backlog.md); rules: [methodology](benchmarking/methodology.md); review: [scaffolding review](reviews/2026-09-28-scaffolding.md).

## 10. Testing status

Latest local gates after review fixes: `just fmt && just lint && just typecheck && just test && just hygiene && uv lock --check` passed; 74 passed, 3 skipped, coverage 92.04%, hygiene clean, lock current. A Starlette/httpx deprecation warning remains non-blocking. The 3 Postgres-dependent tests skipped because `DE_DATABASE_URL` is not configured. No clean-clone run or live DB verification has been completed. Repeat all gates after further changes.

## 11. Observability status

JSON logs and correlation IDs flow across the current API → persisted job → worker → logs path. Host telemetry snapshots CPU/RAM/disk/network with optional GPU fields; initial uncommitted code makes individual host fields nullable on psutil/OS errors, with one AccessDenied test, pending broader verification. Metric naming/point/sink primitives exist; runtime queue depth, wait, state, stage, failure, and episode metrics in [the registry](observability/conventions.md) are not wired to runtime call sites. Distributed tracing remains deferred per [ADR 0008](decisions/0008-observability.md); psutil is recorded in [ADR 0013](decisions/0013-cross-platform-resource-telemetry.md).

## 12. Important decisions and rejected alternatives

- Problem: robot episode data engine (ADR 0003); platform over model-specific optimization.
- PostgreSQL catalog + thin custom queue (ADR 0005); content-addressed local artifacts / ecosystem data formats (ADR 0006); build-thin lineage/run records (ADR 0007).
- Stdlib JSON logging, correlation IDs, file metric direction, defer OTel/Prometheus/Grafana until need (ADR 0008); FastAPI (ADR 0009); modular monolith + separate worker (ADR 0010); 70% coverage floor (ADR 0011); host-based development, containers optional (ADR 0012); psutil host telemetry (ADR 0013).
- No performance approach has been measured or adopted; there are no experiment-backed optimization claims. Alternatives and triggers are recorded in the linked ADRs and [experiment registry](experiments/registry.md).
