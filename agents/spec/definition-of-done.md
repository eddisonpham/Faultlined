# Definition of Done (living)

**Current stage: Scaffolding complete (2026-09-28, tagged `scaffold-complete`); work has started on MVP (stage 2).** Every stage-1 criterion is checked with evidence: the authorized benchmark baseline is committed, the suite passes 96/0 against a live Postgres, and a clean clone runs setup/ci/bench green. Product name: **Faultlined**.

```text
Scaffolding → MVP → Production Baseline → Performance/Scaling → Production Hardening → Resume/Demo Ready
```

Rules:
- A stage is complete only when every criterion is checked **and** evidence is linked (commit, doc, review).
- Criteria may be refined through a PR that explains why (and an ADR if architecture changed). Never silently delete.
- The architect refines stage 2–6 criteria after the problem is chosen (phase 03).

## 1. Scaffolding

Source: [01-scaffolding-instructions.md](01-scaffolding-instructions.md) §3.

**Problem**
- [x] Concrete robotics ML infra problem selected (ADR) — [ADR 0003](../decisions/0003-problem-selection.md) (accepted)
- [x] Users and workflow documented — [problem.md](problem.md) §3, §5
- [x] Relationship to NVIDIA/Tesla/Amazon Robotics/Google Robotics patterns documented — [problem.md](problem.md) §6, [industry-patterns.md](../research/industry-patterns.md)
- [x] Scope boundaries and non-goals explicit — [problem.md](problem.md) §7

**Research**
- [x] Sources recorded in `research/source-log.md` (breadth targets met) — 40 sources / 14 orgs / 13 job postings, [source-log.md](../research/source-log.md)
- [x] Technologies/patterns classified core / optional / excluded with justification — [technology-matrix.md](../research/technology-matrix.md)
- [x] Research sufficient to justify the architecture — validated against [../architecture/overview.md](../architecture/overview.md) + [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md); no evidence gaps found in phase 03

**Architecture**
- [x] Components, responsibilities, data/control flow documented — [components.md](../architecture/components.md), [data-flow.md](../architecture/data-flow.md)
- [x] Major interfaces/APIs defined — [api.md](../architecture/api.md) (phase-05 implementation scope called out)
- [x] Storage, compute, orchestration, model execution, observability, frontend boundaries established — architecture docs; actual implementation limitations are stated in [implementation/status.md](../implementation/status.md)
- [x] Trade-offs documented; every significant choice has an ADR — [trade-offs.md](../architecture/trade-offs.md), ADRs 0003–0013

**Repository**
- [x] Layout documented ([repo-layout.md](../architecture/repo-layout.md)); new code has an obvious home
- [x] Can answer what/why/how components interact/where code goes/how to test/run/benchmark/where decisions live — [README.md](../../README.md), [HANDOFF.md](../HANDOFF.md)

**Agent infrastructure**
- [x] Standards, methodology, conventions, known limitations, rejected approaches, current experiments all in `agents/` — see [benchmark backlog](../benchmarking/backlog.md), [experiment registry](../experiments/registry.md), [implementation status](../implementation/status.md), [observability conventions](../observability/conventions.md), [review](../reviews/2026-09-28-scaffolding.md); the planned EXP-0001 is explicitly not a measured experiment

**Engineering tooling**
- [x] Format, lint, type-check, test, 70% coverage gate, local dev config, CI; containers optional per [ADR 0012](../decisions/0012-host-based-development.md) / machine availability — single canonical workflow `.github/workflows/ci.yml`; local gates pass (96 passed, 0 skipped, 91.59% coverage against a live Postgres)
- [x] One command each for setup, fmt, lint, typecheck, test, bench, run (`justfile`; run/harness code exists and `just bench` compares successfully against the committed baseline (see the benchmarking criterion)

**Vertical slice**
- [x] Minimal end-to-end path through real API → Postgres queue → worker → content-addressed artifact → episode/lineage → API read, with unit/contract tests — [vertical-slice.md](../implementation/vertical-slice.md), tests: [unit](../../tests/unit/), [contract](../../tests/contract/), [Postgres integration/E2E](../../tests/integration/test_ingest_job.py), [E2E](../../tests/e2e/test_ingest_flow.py) (DB-dependent tests run against an isolated `just pg-up` cluster, and against a separate `data_engine_test` database so a running `just run` cannot interfere)

**Benchmarking**
- [x] Metric schema + harness + one real micro-benchmark + baseline. Schema v2, statistics, provenance, validated baseline compare, and the synthetic workload are implemented and gated; **measured baseline committed 2026-09-28** at `f7ffbeb` (P50 0.6084 ms, 10 trials, 0 failures) with owner authorization (runbook in [HANDOFF §13](../HANDOFF.md#13-baseline-runbook-owner-green-flag-required)) — [EXP-0001](../experiments/0001-synthetic-ingest-baseline.md), [review](../reviews/2026-09-28-scaffolding.md)

**Observability**
- [x] Structured logs, correlation IDs across components, metric naming, resource telemetry (degrades without GPU) — JSON logs + correlation across API/queue/worker; host telemetry with nullable per-field fallback and optional GPU; **runtime metrics emitted at call sites** (queue depth, queue/run time, stage duration, failures, ingest counters) to a JSONL sink; [conventions](../observability/conventions.md), `src/data_engine/observability/metrics.py`

**Handoff**
- [x] Repo-wide review accepted; `HANDOFF.md` complete; clean-clone run verified — review verdict is accept-with-follow-ups and the handoff is complete; **live-Postgres verification, clean-clone run, and history-wide secrets scan are all done** (isolated `just pg-up` cluster; clean clone at `02e5925` runs setup/ci/bench green; no credential in any of the 15 commits); it surfaced the `--all-extras` defect now fixed

## 2. MVP

Seed criteria refined 2026-09-28 (phase 03) to reference real components per scaffolding instructions §5; original seed
wording preserved in git history. Rationale recorded with the refinement commit.

- [ ] Full workflow of the chosen problem runs end-to-end on real (small) robotics data — `ingest → validation → indexing → builds` via `jobs`/`worker`, on a public LeRobot/MCAP subset ([../architecture/data-flow.md](../architecture/data-flow.md) §1–2)
- [ ] Job lifecycle: submit, queue, run, retry, timeout, cancel — `jobs` state machine with failure-path tests (failure-modes F4, F6, F7)
- [ ] Dataset/model versioning and lineage recorded — `builds` manifests + `catalog` lineage edges (FR-006–FR-008)
- [ ] Experiments reproducible from recorded provenance — `workloads` run records + rebuild determinism test (NFR-004)
- [ ] API contract tests; coverage gate enforced — `api` + committed `docs/api/openapi.json` drift check (ADR 0009)
- [ ] Minimal UI: jobs, artifacts, status — `frontend` Status/Jobs/Artifacts pages ([../architecture/frontend.md](../architecture/frontend.md))
- [ ] First real benchmark baseline committed — `benchmarks/` `bench-ingest` baseline per [../benchmarking/methodology.md](../benchmarking/methodology.md)

## 3. Production Baseline

- [ ] Failure-mode catalog ([../testing/failure-modes.md](../testing/failure-modes.md)) fully `covered` — no row left `planned`
- [ ] Observability complete per [../observability/conventions.md](../observability/conventions.md); documented log/metric queries for `observability` outputs
- [ ] CI runs lint, types, tests, coverage, hygiene, benchmark regression check (`.github/workflows/`)
- [ ] Production-like local tier ([../architecture/deployment.md](../architecture/deployment.md)) documented; runbooks drafted (backup/restore, worker ops)
- [ ] UI covers jobs, resources, experiments, versions, failures, benchmarks, artifacts (all `frontend` pages)

## 4. Performance / Scaling

- [ ] Bottlenecks identified by profiling (`benchmarks/` + telemetry), each addressed via an experiment record in [../experiments/](../experiments/)
- [ ] Scaling curves measured (worker slots, data size, concurrency) via `benchmarks/` harness
- [ ] NFR targets ([requirements.md](requirements.md) §2) met or explicitly revised with evidence

## 5. Production Hardening

- [ ] Fault injection (worker kill, GPU OOM, disk full, DB restart — failure-modes F5/F9/F10/F12) with recorded outcomes in [../experiments/](../experiments/)
- [ ] Load/soak tests via `benchmarks/`; dependency and secrets audit (ADR [0002](../decisions/0002-secrets-handling.md))
- [ ] Runbooks and troubleshooting final ([../architecture/deployment.md](../architecture/deployment.md) + docs/runbooks)

## 6. Resume / Demo Ready

- [ ] README with architecture diagram (from [../architecture/overview.md](../architecture/overview.md)), quickstart, demo script
- [ ] Every resume metric traces to an experiment record with provenance
- [ ] Interview Q&A / design-defense doc (defends [../architecture/trade-offs.md](../architecture/trade-offs.md))
- [ ] Final repo-wide review ([../reviews/](../reviews/)); clean history
