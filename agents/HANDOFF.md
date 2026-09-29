# Handoff

**Status: Scaffolding; review verdict is accept-with-follow-ups (2026-09-28).** The project is **Faultlined**. All engineering findings are resolved and gated; the synthetic-ingest benchmark baseline is measured and committed (2026-09-28, owner-authorized — [§13](#13-baseline-runbook-owner-green-flag-required)). See [engineering review](reviews/2026-09-28-scaffolding.md) and [definition of done](spec/definition-of-done.md). All work is committed on `main` and pushed. Do not tag `scaffold-complete` until the residual verifications below are closed.

**Update 2026-09-29 (stage 2 / MVP).** Four slices landed on `main` since the scaffolding tag: the minimal server-rendered UI ([ADR 0014](decisions/0014-minimal-ui-server-rendered.md)), the committed OpenAPI contract with a drift gate, the job lifecycle — bounded retries, job deadlines, cooperative cancellation, and terminal-vs-retryable failure classification ([ADR 0015](decisions/0015-cooperative-job-lifecycle.md)) — and **real-format ingest**: a LeRobot reader covering v2.1 and v3.0, exercised end to end against real Hub datasets.

**Update 2026-09-29 (validation + run intelligence).** Two further slices landed: **validation** — hashed JSON validation profiles, a 7-rule engine, quarantine and re-validate without re-ingest ([ADR 0016](decisions/0016-json-validation-profiles.md)) — and **run intelligence** ([slice plan](implementation/run-intelligence-slice.md), [EXP-0002](experiments/0002-run-intelligence-workloads.md)): runtime metrics aggregation and `GET /api/v1/metrics` ([ADR 0017](decisions/0017-runtime-metrics-aggregation.md)), episode motion-quality analytics computed at ingest ([ADR 0018](decisions/0018-episode-quality-signals.md)), an instrument UI (`/ui/metrics`, `/ui/episodes`, `/ui/episodes/{id}`, `/ui/insights`), and run-inspection API surfaces — episode catalog with quality filters, per-episode validation verdicts and violations, job → episodes reverse lineage — each latency-measured in isolated benchmark processes ([ADR 0019](decisions/0019-benchmark-workload-isolation.md)). The suite is at 308 passing with 92.23% coverage, 22 of them against a live Postgres. The next MVP criteria are the rest of the `ingest → validation → indexing → builds` workflow: indexing and builds are still open, and MCAP ingest is the remaining format.

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
- Postgres schema/repository for jobs, artifacts, episodes, and lineage; idempotent submission and atomic queue claim; queue-depth query; **job lifecycle** — attempt counting in the claim, `max_attempts` / `deadline_seconds` on submission, cooperative cancel, deadline sweep, and terminal-vs-retryable failure classification (ADR 0015).
- **Real-format ingest**: `ingest_source` job type + a LeRobot reader (v2.1 and v3.0) behind a sniff-based registry, verified end to end against `lerobot/svla_so101_pickplace` and `yaak-ai/lerobot-driving-school`. Episode identity is `(source_hash, episode_key)`, so many episodes in one Parquet shard each get a catalog row while the file itself is stored once.
- Minimal server-rendered UI at `/ui` (Status / Jobs / Artifacts) with a committed OpenAPI contract and a drift check in `just ci` (ADR 0014).
- One ingest worker process; atomic SHA-256 filesystem artifact writes; synthetic JSON canonicalization and lineage readback.
- Unit/contract/E2E tests and Postgres-marked integration tests.
- JSON log formatter, request/job correlation context, psutil/NVML host telemetry with nullable fallbacks.
- **Runtime metrics emitted at call sites**: queue depth, queue/run time, stage duration, failures by reason code, retries, cancellations, timeouts, episode and artifact counters, API request latency, catalog query timing, worker heartbeats (`RuntimeMetrics`, JSONL sink configured by `DE_METRICS_PATH`). Read-back aggregation into percentile summaries, time-bucketed series, and heartbeat ages serves `GET /api/v1/metrics` and the UI (ADR 0017).
- **Validation and quality**: hashed JSON validation profiles with a 7-rule engine, quarantine + re-validate (ADR 0016); motion-quality signals (movement, jerk, stall ratio, length z-score, absolute σ-band verdict) computed at ingest and persisted per episode (ADR 0018).
- **Run inspection**: `GET /api/v1/episodes` (state/quality-flag filters), `GET /api/v1/episodes/{id}/quality` and `/validation`, `GET /api/v1/quality/summary`, `GET /api/v1/jobs/{id}/episodes` (reverse lineage), `GET /api/v1/jobs/{id}/report` (run triage card, also on the job page); UI pages `/ui/metrics`, `/ui/episodes`, `/ui/episodes/{id}`, `/ui/insights`.
- Benchmark schema v2, deterministic statistics, provenance capture, and a CLI that validates results and baselines, refuses to publish from a failed run, and fails closed with an actionable message.

**Not implemented or incomplete:**
- MCAP ingest, Parquet indexing, LeRobot v3 builds, workload execution, retry backoff, worker leases and lease-based heartbeats (heartbeat *records* exist), mid-run cancellation, GC, formal DB migrations (schema changes are still inline idempotent DDL plus an `ALTER TABLE` block), and committed baselines for the run-intelligence workloads (measured in EXP-0002; committing them needs the owner's green flag).

See [implementation status](implementation/status.md), [failure modes](testing/failure-modes.md), and [review findings](reviews/2026-09-28-scaffolding.md).

## 6. Known technical risks

1. **Benchmark baseline is measured and committed** (2026-09-28, authorized): P50 0.6084 ms, 10 trials, 0 failures at `f7ffbeb`. See [§13](#13-baseline-runbook-owner-green-flag-required) and [EXP-0001](experiments/0001-synthetic-ingest-baseline.md). It is a regression tripwire, not a performance target: observed run-to-run spread was ~24%.
2. **Hardware matching is exact.** A baseline only compares on a byte-identical CPU/RAM/GPU/OS/disk profile; on a different machine the harness refuses to compare. Revisit with evidence before gating CI.
3. **Postgres path is now verified.** `just pg-up` starts an isolated cluster in gitignored `var/pgdata` on port 55432 using local trust auth, so the full suite runs with nothing skipped and the DSN holds no credential. The owner's system PostgreSQL on 5432 is untouched and its superuser password remains unknown; that instance is still unverified.
4. **Crash consistency:** artifact publication and DB catalog writes are separate transactions; worker crash recovery, retries, leases, and orphan GC remain future work.
5. **Clean-clone validation and the history-wide secrets scan are done** (2026-09-28). A clean clone runs `just setup`, `just ci` (95 passed) and `just bench` green; 15 commits contain no credential, `.env` was never tracked, and the only DSN in history is the `USER:PASSWORD` placeholder. No `.env` was opened. This surfaced the `--all-extras` defect: `uv run` pruned the GPU extra, so a fresh clone reported no GPU and `just bench` refused to compare.

## 7. Highest-priority next steps

1. ~~Baseline runbook~~ — done 2026-09-28 with owner authorization ([§13](#13-baseline-runbook-owner-green-flag-required)). New workload baselines (EXP-0002 backlog rows B-003/B-012–B-015) need the same green flag before any is committed.
2. Optionally verify the slice against the owner's own system PostgreSQL (port 5432) by putting its DSN in `.env`; the isolated `just pg-up` cluster already covers this.
3. ~~Clean-clone validation and secrets scan~~ — done 2026-09-28. The stage criteria are now all evidenced; the owner may consider `scaffold-complete`.
4. MVP engineering order: ~~real LeRobot reader~~ (done) → ~~rule-based validation/quarantine~~ (done) → Parquet metadata index → durable job lifecycle/recovery → deterministic LeRobot v3 dataset build → MCAP reader.

## 8. Open architectural questions

- What small public MCAP/LeRobot data subset will be versioned and used as the reproducible MVP fixture?
- What exact catalog migrations and transaction/outbox strategy are needed to reconcile artifact writes with database state?
- What retry/lease/cancel semantics and worker concurrency limits will be implemented first?
- Should runtime metrics initially remain JSONL files or move to a database/exporter once actual query needs exist? Follow ADR 0008 triggers; avoid adding a telemetry service speculatively.
- What machine/hardware profile and owner-approved run conditions define the first committed baseline?

## 9. Benchmarking status

Harness and schema v2 are implemented and tested; `synthetic-episode-ingest` measures a tiny synthetic episode using an in-memory catalog and the filesystem artifact store (3 warmups, 10 trials). It is **not** a database, API, MCAP, LeRobot, or production throughput benchmark. Its first baseline was measured and committed on 2026-09-28 with owner authorization ([§13](#13-baseline-runbook-owner-green-flag-required), [EXP-0001](experiments/0001-synthetic-ingest-baseline.md)); it is a regression tripwire, not a performance target. A named `WORKLOADS` registry (`--workload`) adds run-intelligence workloads — real LeRobot ingest, quality analysis, validation evaluation, metrics aggregation, API/UI latency — measured **one workload per process** ([ADR 0019](decisions/0019-benchmark-workload-isolation.md); batching in one process inflates p50 ~24× on Windows) and recorded with raw samples in [EXP-0002](experiments/0002-run-intelligence-workloads.md). No new baselines are committed without owner authorization. Backlog: [benchmark backlog](benchmarking/backlog.md); rules: [methodology](benchmarking/methodology.md); review: [scaffolding review](reviews/2026-09-28-scaffolding.md).

## 10. Testing status

Latest local gates (2026-09-29, `just ci`): format, lint, type-check, tests, hygiene, and OpenAPI drift all pass; **308 passed, 92.23% coverage** against the isolated Postgres cluster (22 integration tests run when `DE_DATABASE_URL` is set and skip cleanly without it), hygiene clean, lock current. A Starlette/httpx `TestClient` deprecation warning is non-blocking and deferred. Live DB verification, clean-clone validation, and the history-wide secrets scan are all done. Repeat all gates after any change.

## 11. Observability status

JSON logs and correlation IDs flow across the current API → persisted job → worker → logs path. Host telemetry snapshots CPU/RAM/disk/network with optional GPU fields; host fields become nullable on psutil/OS errors, with an AccessDenied test in the passing suite. **Runtime metrics are emitted** at the worker and ingest call sites — `jobs_queue_depth`, `jobs_queue_time_seconds`, `jobs_run_time_seconds`, `jobs_failures_total`, `pipeline_stage_duration_seconds`, `episodes_ingested_total`, `artifacts_written_bytes_total` — written to a JSONL sink at `DE_METRICS_PATH` (default `var/metrics/runtime.jsonl`). Labels stay low-cardinality; IDs appear only in logs. Sink failures never break the pipeline. API request latency and catalog query timing are instrumented the same way, and `observability/aggregate.py` reads the sink back into percentile summaries, time-bucketed series, and worker heartbeat ages for `GET /api/v1/metrics` and `/ui/metrics` ([ADR 0017](decisions/0017-runtime-metrics-aggregation.md)). Distributed tracing remains deferred per [ADR 0008](decisions/0008-observability.md); psutil is recorded in [ADR 0013](decisions/0013-cross-platform-resource-telemetry.md).

## 12. Important decisions and rejected alternatives

- Problem: robot episode data engine (ADR 0003); platform over model-specific optimization.
- PostgreSQL catalog + thin custom queue (ADR 0005); content-addressed local artifacts / ecosystem data formats (ADR 0006); build-thin lineage/run records (ADR 0007).
- Stdlib JSON logging, correlation IDs, file metric direction, defer OTel/Prometheus/Grafana until need (ADR 0008); FastAPI (ADR 0009); modular monolith + separate worker (ADR 0010); 70% coverage floor (ADR 0011); host-based development, containers optional (ADR 0012); psutil host telemetry (ADR 0013).
- No performance approach has been measured or adopted; there are no experiment-backed optimization claims. Alternatives and triggers are recorded in the linked ADRs and [experiment registry](experiments/registry.md).

## 13. Baseline runbook (owner green flag required)

**Status: executed 2026-09-28 with owner authorization.** Baseline written at commit `f7ffbeb` (clean tree); verify pass reported no regression. One defect was found and fixed in this runbook while executing it: the command originally wrote a SHA-named file, but `DEFAULT_BASELINE` in `benchmarks/harness.py` is the fixed path `synthetic-ingest-windows.json`, so a SHA-named file would never be read by bare `just bench` and step 4 would have failed. Always write the default path, or pass the same `--baseline` to both invocations.

Do not start until the owner authorizes a measured baseline. Preconditions: a committed, clean tree; the machine you
will measure on must stay idle; nothing else heavy may run during the measurement.

```bash
just ci                                  # 1. all gates green before measuring
git status --short                       # 2. must be empty (provenance records dirty state)
just bench --write-baseline             # 3. writes benchmarks/baselines/synthetic-ingest-windows.json (the fixed default)
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
