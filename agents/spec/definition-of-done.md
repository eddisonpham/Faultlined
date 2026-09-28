# Definition of Done (living)

**Current stage: Scaffolding**  <!-- update when a stage is accepted; also update CLAUDE.md -->

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
- [ ] Components, responsibilities, data/control flow documented
- [ ] Major interfaces/APIs defined
- [ ] Storage, compute, orchestration, model execution, observability, frontend boundaries established
- [ ] Trade-offs documented; every significant choice has an ADR

**Repository**
- [ ] Layout documented (`architecture/repo-layout.md`); new code has an obvious home
- [ ] Can answer: what/why/how components interact/where code goes/how to test/run/benchmark/where decisions live

**Agent infrastructure**
- [ ] Standards, methodology, conventions, known limitations, rejected approaches, current experiments all in `agents/`

**Engineering tooling**
- [ ] Format, lint, type-check, test, coverage gate, local dev, env config, CI; containers optional per [ADR 0012](../decisions/0012-host-based-development.md) / machine availability
- [ ] One command each for: setup, fmt, lint, typecheck, test, bench, run

**Vertical slice**
- [ ] Minimal end-to-end path through the real architecture, with tests

**Benchmarking**
- [ ] Metric schema + harness + one real micro-benchmark + baseline; placeholders listed in backlog

**Observability**
- [ ] Structured logs, correlation IDs across components, metric naming, resource telemetry (degrades without GPU)

**Handoff**
- [ ] Repo-wide review done; `HANDOFF.md` complete; clean-clone run verified

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
