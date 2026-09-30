# Requirements

**Status: written (phase 02, 2026-09-28).** Derived from [problem.md](problem.md) (robot episode data engine,
ADR [0003](../decisions/0003-problem-selection.md)). Every requirement traces to a persona in problem.md §3
(`ML-ENG`, `DATA-ENG`, `EVAL-ENG`, `PLATFORM`). Numeric targets are **provisional until measured** — each carries a
"how we'll validate" note per [../benchmarking/methodology.md](../benchmarking/methodology.md); revise with evidence
(ADR if architecture changes).

## 1. Functional requirements

| ID | Requirement | Persona / trace | Rationale |
|---|---|---|---|
| FR-001 | Ingest raw episodes (MCAP files, LeRobot v2/v3 dataset dirs) as jobs, with idempotency keys so duplicate submissions are safe | DATA-ENG | "Turn today's robot recordings into trusted catalog entries" (problem.md §3) |
| FR-002 | Validate each episode against a declarative **validation profile** (required channels/keys, schema, monotonic time, frequency bounds, NaN/latency checks) and record pass/fail with stable reason codes | DATA-ENG | Quality gating is the heart of the data engine (#9, #14 in source-log) |
| FR-003 | Quarantine failed episodes with reasons; allow re-validation after remediation without re-ingesting bytes | DATA-ENG | Failure triage must not require full re-ingest |
| FR-004 | Automatically index episodes after validation: episode-level metadata + frame statistics into catalog (Postgres) + Parquet metadata tables | DATA-ENG, ML-ENG | Curation requires an index (industry-patterns §2.5) |
| FR-005 | Query episodes by metadata predicates (task, robot/embodiment, time range, duration, quality flags) to define a **selection** | ML-ENG | "Assemble dataset builds from queries" (problem.md §3) |
| FR-006 | Build **dataset builds** from selections: content-addressed, exported in LeRobot v3, with a lineage manifest (source episode IDs + hashes, validation profile + version, code commit, config, timestamps) | ML-ENG, EVAL-ENG | "A versioned, curated dataset I can cite in a run record" (problem.md §3) |
| FR-007 | Rebuild determinism: re-running a build with the same recorded provenance yields an identical content hash | ML-ENG, EVAL-ENG | Reproducibility is the product promise (problem.md §8.2) |
| FR-008 | Lineage queries both directions: build → source episodes + validation results; episode → builds containing it | ML-ENG, EVAL-ENG | Traceability (problem.md §8.3; OSMO lineage pattern #14) |
| FR-009 | Job lifecycle for every pipeline stage: submit, queue, run, retry (bounded, with backoff), timeout, cancel — with observable state at all times | PLATFORM | Spec §4; problem.md §5 |
| FR-010 | Worker pool with heartbeats; jobs survive process restart (requeued), duplicate execution prevented where promised | PLATFORM | Fault tolerance (spec §4); stage ≥ Production Baseline expects failure-mode coverage |
| FR-011 | Run records for workloads (model/eval jobs): config, dataset build hash, code commit, env capture, seeds, metrics — enough to reproduce per §5 | EVAL-ENG | Spec §9; "Freeze the data a regression was measured on" (problem.md §3) |
| FR-012 | Narrow **workload interface**: registered workload types run against dataset builds and emit artifacts + run records; models are workloads, never the product | EVAL-ENG | CLAUDE.md non-negotiable; spec §3 |
| FR-013 | HTTP API exposing all of the above with documented contracts (OpenAPI), pagination, error model, idempotency | PLATFORM, all | Spec §4 "clean APIs"; contract tests are an MVP criterion |
| FR-014 | Structured JSON logs with correlation IDs (ingest_id / job_id / run_id / episode_id) across all components | PLATFORM | Definition-of-done: correlation IDs across components |
| FR-015 | Resource telemetry (GPU via NVML, CPU/RAM/disk) collected periodically and degrading gracefully when no GPU | PLATFORM | Definition-of-done: resource telemetry (degrades without GPU) |
| FR-016 | Engineering UI: browse episodes, datasets, builds, lineage, jobs/queue, failures, benchmark results; submit/cancel jobs | all | Spec §4; MVP: "Minimal UI: jobs, artifacts, status" |
| FR-017 | Benchmark harness measuring engine stages (ingest throughput, validation latency, query latency, build time) with persisted results and baselines | PLATFORM | Definition-of-done: metric schema + harness + one real micro-benchmark + baseline |
| FR-018 | Dataset builds and artifacts stored with content addressing; artifacts retrievable by hash; storage layout documented | ML-ENG | FR-006/007 foundation; caching and dedup depend on it |

## 2. Non-functional requirements (provisional until measured)

| ID | Requirement (target) | Status | How we'll validate |
|---|---|---|---|
| NFR-001 | **Revised 2026-09-30 (owner decision) from a single ≥ 50 MB/s whole-pipe target to per-stage targets**, because [EXP-0005](../experiments/0005-mcap-ingest-flatten-plan.md) measured that container iteration + JSON decode alone run at 42.6 MiB/s before the engine works, so the old target is unreachable by this design: (a) container iteration ≥ 100 MiB/s (measured 131), (b) JSON payload decode ≥ 35 MiB/s (measured 42.6), (c) engine whole-ingest ≥ 10 MiB/s P50 on the B-002 fixture (measured 10.8–18.3). A decode-avoiding reader (binary CDR/protobuf interpretation) is the recorded path back to ≥ 50 MB/s and remains deferred | revised with evidence | Bench `mcap-ingest` (B-002) plus EXP-0005's per-stage attribution method; baselines committed |
| NFR-002 | Validation latency p95 ≤ 2 s per ≤ 60 s episode (rule-based checks) | provisional | Bench `validate-latency`, report p50/p95/p99 over ≥ 100 episodes |
| NFR-003 | Metadata query latency p95 ≤ 200 ms over a 10k-episode catalog | provisional | Bench `query-latency` with seeded 10k-row catalog |
| NFR-004 | Build determinism: identical content hash on rebuild — **100% or the release fails** | hard | Test (not a bench): rebuild twice in CI, compare manifests/hashes |
| NFR-005 | Enqueue→start p95 ≤ 1 s at 100 queued jobs; cancel takes effect ≤ 2 s | provisional | Bench `job-lifecycle`; failure-path tests for cancel/timeout |
| NFR-006 | No catalog/artifact loss on unclean shutdown; pending jobs recovered on restart | hard | Fault-path tests (kill -9 worker during ingest/validate/build) |
| NFR-007 | Whole platform fits 31.4 GB RAM and (if used) ≤ 8 GB VRAM; runs with GPU absent | hard | Resource telemetry in bench runs; CI runs GPU-less path |
| NFR-008 | Working-set scale: 10k episodes / ~500 GB raw data handled without architectural change | provisional | Seeded-scale bench in the Performance/Scaling stage; revise with evidence |
| NFR-009 | Restart recovery ≤ 2 min for the local deployment tier; no manual state repair | provisional | Documented runbook drill (Production Baseline stage) |
| NFR-010 | API latency p95 ≤ 300 ms for CRUD/list endpoints at modest load (10 rps) | provisional | Bench `api-latency`; contract tests in CI |
| NFR-011 | Deterministic tooling: setup, fmt, lint, typecheck, test, bench, run each via one command | hard | Phase 04 tooling; CI executes all of them |

## 3. Failure-mode catalog

The authoritative catalog lives in `agents/testing/failure-modes.md` (drafted in phase 03, refined with tests).
Required failure classes at minimum, each mapped to component + planned test:

- Corrupt/missing episode data (truncated MCAP, missing channels, bad timestamps) → FR-002 reason codes
- Invalid configuration / validation profile → API 4xx + test
- Duplicate job submission → idempotency keys (FR-001/FR-013)
- Worker failure mid-job → heartbeat timeout → requeue (FR-010); at-least-once stance documented in ADR 0005
- Timeout and cancellation races (cancel during retry, timeout after completion) → FR-009
- Partial pipeline execution (ingest succeeded, index failed) → resumable stage transitions, tests
- Out-of-disk / artifact write failure → transactional catalog + failed job state, tests
- GPU OOM / no-GPU path in workloads and telemetry → degrade gracefully (NFR-007)
- Restart with in-flight jobs → recovery tests (NFR-006)

## 4. Data contracts

| Contract | Format | Validation rules (summary) |
|---|---|---|
| Raw episode (log) | MCAP container (#36) | Non-empty; schema records resolvable; per-channel monotonic non-decreasing timestamps; required channels per validation profile; duration ≥ profile minimum |
| Raw episode (dataset dir) | LeRobot v2/v3 (#34) | Metadata JSON/Parquet parseable; episode boundaries consistent with Parquet/MP4 lengths; action/observation keys per profile |
| Validation profile | YAML (declarative) | Versioned; required keys present; bounds well-formed; profile hash recorded in lineage |
| Curation query | Restricted predicate DSL (JSON/YAML): equality, ranges, IN, AND/OR over indexed metadata fields | Field allow-list; query plan bounded (no arbitrary code); queries recorded in build manifests |
| Episode metadata (indexed) | Postgres rows + Parquet tables | Stable schema (episode_id, source_hash, task, robot, t_start/t_end, duration, channel stats, quality flags, validation result + version) |
| Dataset build manifest | JSON | Content hash (sha256 over canonical serialization); sorted source list (episode_id + source_hash); profile hash/version; code commit; config; created_at; seeds where applicable |
| Curated dataset build | LeRobot v3 directory | Passes the LeRobot v3 structural contract; reproducible byte layout (canonical ordering) |
| Run record | JSON/Postgres row | Per spec §9 fields (see §5) |
| API | HTTP+JSON, OpenAPI spec | Error model: machine-readable `code`, `message`, `details`; pagination: cursor-based; idempotency: `Idempotency-Key` header on mutating job submission |
| Job state | Enum | `queued → running → succeeded|failed|canceled|timed_out`, `queued→canceled`, `running→retrying→queued` (bounded attempts) |

## 5. Reproducibility requirements

Every run record and dataset build manifest must capture (spec §9):

1. Git commit (and dirty-flag) of the engine code
2. Full configuration (validation profile hash, query, build params) — canonical serialization
3. Dataset/build identity: content hash + source episode hashes (never a mutable name alone)
4. Model/workload identity when applicable (name + version + artifact hash)
5. Hardware + software environment capture (OS, Python, package lock hash; GPU model if present)
6. Random seed(s) wherever any stochastic step exists
7. Timestamps (UTC) and correlation IDs
8. Metrics in the [../benchmarking/metric-schema.md](../benchmarking/metric-schema.md) format

Determinism rules: sorted iteration over sets of episodes; stable content hashing (canonical JSON, sha256); no wall-clock
or locale-dependent values inside hashed content; pinned dependency versions (lockfile).

## 6. Constraints

- **Hardware** ([environment.md](environment.md)): 1× RTX 5060 8 GB, 24-core CPU, 31.4 GB RAM, ~228 GB free NVMe,
  Windows 11 + Git Bash, **no Docker**, Python 3.14 / Node 24 / Rust 1.98 / CMake 4.1, **no g++**. Design targets local
  development on this box; production-like local tier must run on the same box without containers.
- **Scope guard** (CLAUDE.md): no quantization / TensorRT / ONNX / edge optimization as a theme; no duplication of hand
  perception, VLA quantization, or grasp deployment projects.
- **Secrets**: env vars only (`HF_KEY`), never committed ([ADR 0002](../decisions/0002-secrets-handling.md)).
- **Dependencies**: every dependency justified in [../research/technology-matrix.md](../research/technology-matrix.md)
  and the technology decision matrix ([../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md)).
- **Windows-first dev**: scripts run under Git Bash; no Linux-only syscalls; paths forward-slash safe.

## 7. Explicitly deferred requirements

| Deferred | Trigger to revisit | Why deferred now |
|---|---|---|
| Embedding-based episode mining / failure clustering (candidate C core) | Metadata catalog + curation loop stable (stage ≥ MVP) | Needs A's index first; retrieval-quality evaluation is fuzzy (problem-candidates.md) |
| Policy evaluation & regression platform (candidate B) | Dataset versioning contract real and used by ≥ 1 workload | Needs versioned scenario data first; sim-based eval impossible on current hardware |
| S3/MinIO object-storage tier | Data volume or multi-machine requirement | 228 GB local NVMe suffices; OSMO-style interchange is the later pattern |
| ROS 2 bag reader adapter | User need for pre-MCAP recordings | MCAP is ROS 2's default log format already |
| Auto-labeling / learned quality rules | Rule-based validation coverage plateaued | ML quality is research-shaped; rules first |
| Multi-user auth / RBAC | Second real user | Local single-user tier; OIDC patterns exist (#14) for later |
| Hugging Face Hub export of builds | Curated builds worth publishing | Distribution nicety |
| OTel traces, Prometheus/Grafana | ≥ 3 traced components or an ops dashboard need | JSON logs + file metrics satisfy current conventions |
| Frontend framework final selection | Phase 12 frontend research | UI scope fixed in architecture; visuals decided there |
