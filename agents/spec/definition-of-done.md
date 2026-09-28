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
- [ ] Concrete robotics ML infra problem selected (ADR)
- [ ] Users and workflow documented
- [ ] Relationship to NVIDIA/Tesla/Amazon Robotics/Google Robotics patterns documented
- [ ] Scope boundaries and non-goals explicit

**Research**
- [ ] Sources recorded in `research/source-log.md` (breadth targets met)
- [ ] Technologies/patterns classified core / optional / excluded with justification
- [ ] Research sufficient to justify the architecture

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
- [ ] Format, lint, type-check, test, coverage gate, local dev, env config, CI, containerization (where appropriate)
- [ ] One command each for: setup, fmt, lint, typecheck, test, bench, run

**Vertical slice**
- [ ] Minimal end-to-end path through the real architecture, with tests

**Benchmarking**
- [ ] Metric schema + harness + one real micro-benchmark + baseline; placeholders listed in backlog

**Observability**
- [ ] Structured logs, correlation IDs across components, metric naming, resource telemetry (degrades without GPU)

**Handoff**
- [ ] Repo-wide review done; `HANDOFF.md` complete; clean-clone run verified

## 2. MVP (seed criteria)

- [ ] Full workflow of the chosen problem runs end-to-end on real (small) robotics data
- [ ] Job lifecycle: submit, queue, run, retry, timeout, cancel
- [ ] Dataset/model versioning and lineage recorded
- [ ] Experiments reproducible from recorded provenance
- [ ] API contract tests; coverage gate enforced
- [ ] Minimal UI: jobs, artifacts, status
- [ ] First real benchmark baseline committed

## 3. Production Baseline (seed criteria)

- [ ] Failure-mode catalog (`testing/`) covered by tests
- [ ] Observability complete per conventions; dashboards/queries documented
- [ ] CI runs lint, types, tests, coverage, hygiene, benchmark regression check
- [ ] Deployment (production-like local) documented; runbooks drafted
- [ ] UI covers jobs, resources, experiments, versions, failures, benchmarks, artifacts

## 4. Performance / Scaling (seed criteria)

- [ ] Bottlenecks identified by profiling, each addressed via an experiment record
- [ ] Scaling curves measured (workers, data size, concurrency)
- [ ] Requirements' performance targets met or explicitly revised with evidence

## 5. Production Hardening (seed criteria)

- [ ] Fault injection (worker kill, OOM, network partitions, disk full) with recorded outcomes
- [ ] Load/soak tests; dependency and secrets audit
- [ ] Runbooks and troubleshooting final

## 6. Resume / Demo Ready (seed criteria)

- [ ] README with architecture diagram, quickstart, demo script
- [ ] Every resume metric traces to an experiment record with provenance
- [ ] Interview Q&A / design-defense doc
- [ ] Final repo-wide review; clean history
