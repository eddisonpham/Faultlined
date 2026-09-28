# Technology Decision Matrix

Phase 02 (2026-09-28). Scores are 1 (worst) – 5 (best) per criterion. **Complexity is scored inverted: 5 = simplest option.**
Weights sum to 100%:

| Criterion | Weight | Meaning |
|---|---|---|
| IR — Industry relevance | 10% | Used by the orgs in [../research/source-log.md](../research/source-log.md) for this problem |
| TN — Technical necessity | 20% | Needed to meet [../spec/requirements.md](../spec/requirements.md) — **veto if < 3** |
| P — Performance | 10% | Adequate throughput/latency for the NFR targets |
| LF — Local feasibility | 20% | Works on captured hardware ([../spec/environment.md](../spec/environment.md)) — **veto if < 3** |
| CX — Simplicity | 15% | 5 = fewest moving parts (inverted complexity) |
| MT — Maintainability | 15% | Testability, clarity, ecosystem health |
| RR — Resume relevance | 10% | Defensible talking point (never a driver) |

**Veto rule:** TN < 3 or LF < 3 disqualifies an option regardless of weighted score. **Prefer the simplest option that
meets requirements on the captured hardware** (phase-02 mandate). Full technology inventory with core/optional/excluded
classes: [../research/technology-matrix.md](../research/technology-matrix.md).

## 1. Primary language(s)

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **Python 3.14** | 5 | 5 | 4 | 5 | 5 | 5 | 4 | **4.80** | **chosen** — ADR [0004](../decisions/0004-language-and-toolchain.md) |
| Rust | 3 | 2 | 5 | 3 | 2 | 3 | 4 | 2.95 | veto (TN<3): no measured hot path; deferred with trigger |
| C++ | 3 | 2 | 5 | 1 | 2 | 3 | 3 | 2.45 | veto (TN<3, LF<3): no compiler on the box; deferred with trigger |

Deferral entry: C++/Rust **deferred until** profiling shows reader/decoder-bound throughput below NFR-001 (record in
`agents/experiments/` first). The robotics-ML data ecosystem (LeRobot #34, MCAP readers #36, Rerun #38) is Python-first.

## 2. API style / framework

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **FastAPI + Pydantic (HTTP+JSON, OpenAPI)** | 5 | 5 | 4 | 5 | 5 | 5 | 4 | **4.80** | **chosen** — ADR [0009](../decisions/0009-api-style.md) |
| Flask + marshmallow | 3 | 4 | 3 | 5 | 4 | 4 | 2 | 3.80 | rejected: hand-rolled validation/OpenAPI |
| gRPC / protobuf | 4 | 2 | 5 | 4 | 2 | 3 | 4 | 3.25 | veto (TN<3): no streaming RPC requirement yet |

## 3. Job queue / scheduler

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **Postgres-backed thin queue + worker pool (build)** | 4 | 5 | 4 | 5 | 4 | 5 | 4 | **4.55** | **chosen** — ADR [0005](../decisions/0005-catalog-and-job-queue.md) |
| Celery/RQ (+ Redis broker) | 3 | 3 | 4 | 4 | 3 | 4 | 3 | 3.45 | rejected: extra broker process; opaque lifecycle semantics we must own |
| Temporal | 3 | 2 | 4 | 3 | 2 | 3 | 4 | 2.85 | veto (TN<3): durable-workflow engine heavier than the workflow |
| Ray | 4 | 2 | 5 | 2 | 2 | 3 | 5 | 2.95 | veto (TN<3, LF<3): distributed compute framework on one machine |
| Kubernetes + Argo Workflows | 4 | 1 | 4 | 1 | 1 | 3 | 5 | 2.30 | veto (TN<3, LF<3): no Docker/K8s locally; would outsource the deliverable |

## 4. Metadata store

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **PostgreSQL** | 5 | 5 | 4 | 5 | 4 | 5 | 4 | **4.55** | **chosen** — ADR [0005](../decisions/0005-catalog-and-job-queue.md) |
| SQLite | 3 | 5 | 4 | 5 | 5 | 3 | 2 | 4.10 | rejected: single-writer concurrency limits under worker pool + API |
| DuckDB (as system of record) | 3 | 3 | 5 | 5 | 4 | 3 | 3 | 3.75 | rejected as OLTP store; retained as **optional** analytical add-on (deferred until queries outgrow SQL over Parquet) |

## 5. Artifact storage and data formats

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **Local FS, content-addressed; MCAP + LeRobot v3 + Parquet/Arrow** | 5 | 5 | 4 | 5 | 5 | 5 | 4 | **4.80** | **chosen** — ADR [0006](../decisions/0006-storage-and-formats.md) |
| S3/MinIO object store from day one | 4 | 2 | 4 | 3 | 2 | 4 | 4 | 3.10 | veto (TN<3): 228 GB local NVMe suffices; deferred until volume/multi-machine trigger |
| Delta Lake / Apache Iceberg | 3 | 1 | 4 | 2 | 1 | 3 | 4 | 2.30 | veto (TN<3, LF<3): lakehouse engines (Spark/Trino) far beyond scale |
| Inventing a bespoke episode format | 1 | 1 | 3 | 4 | 2 | 2 | 1 | 1.85 | veto: permanent translation tax (industry-patterns §2.2) |

## 6. Dataset & model versioning + lineage

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **Build-thin: content hashes + manifests in the catalog** | 4 | 5 | 4 | 5 | 4 | 5 | 4 | **4.55** | **chosen** — ADR [0007](../decisions/0007-lineage-and-run-records.md) |
| DVC | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3.00 | rejected: git-pointer model fits source trees, not GB-scale binary episodes |
| lakeFS | 3 | 2 | 3 | 2 | 2 | 3 | 4 | 2.55 | veto (TN<3): object-store versioning; no object store yet |

## 7. Experiment tracking (build vs adopt)

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **Build-thin run records in the catalog** | 4 | 5 | 4 | 5 | 4 | 5 | 4 | **4.55** | **chosen** — ADR [0007](../decisions/0007-lineage-and-run-records.md) |
| Adopt MLflow now | 4 | 3 | 4 | 4 | 3 | 4 | 4 | 3.65 | deferred until run-record schema stabilizes and outgrows the catalog |
| Weights & Biases (hosted) | 4 | 2 | 4 | 3 | 4 | 3 | 4 | 3.25 | veto (TN<3): hosted SaaS conflicts with local-first reproducibility |

## 8. Observability stack

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **Stdlib JSON logs + psutil host telemetry + optional pynvml + file metrics** | 4 | 5 | 4 | 5 | 4 | 5 | 3 | **4.45** | **chosen** — ADRs [0008](../decisions/0008-observability.md), [0013](../decisions/0013-cross-platform-resource-telemetry.md) |
| OTel + Prometheus + Grafana now | 5 | 2 | 4 | 3 | 2 | 3 | 5 | 3.15 | veto (TN<3): 1 process + workers, nothing to scrape/trace yet; deferred |
| structlog / loguru now | 3 | 3 | 4 | 5 | 4 | 4 | 3 | 3.80 | optional later; stdlib JSON formatter covers current needs |

## 9. Frontend framework

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| React + Vite + TypeScript | 5 | 4 | 4 | 4 | 3 | 4 | 4 | 4.05 | **deferred until phase 12** (frontend research owns the choice) |
| Svelte / SvelteKit | 4 | 3 | 4 | 4 | 4 | 3 | 3 | 3.45 | deferred until phase 12 |
| Server-rendered (FastAPI + templates) | 3 | 4 | 4 | 5 | 5 | 4 | 2 | 4.05 | deferred until phase 12; fallback if a JS tree proves unjustified |

Deferral entry: **deferred until phase 12** — UI *scope and API boundary* are fixed in phase 03
(`overview.md`/`frontend.md`); only the framework/visual layer is undecided. Trigger owners: frontend-engineer role.

## 10. Containerization / compose

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **No containers now; host tooling** | 3 | 4 | 4 | 5 | 5 | 4 | 2 | **4.05** | **chosen** — ADR [0012](../decisions/0012-host-based-development.md) |
| Docker + docker-compose required | 5 | 2 | 3 | 1 | 2 | 4 | 4 | 2.70 | veto (LF<3): no container runtime on the dev box (environment.md) |
| Podman | 3 | 2 | 3 | 2 | 2 | 3 | 2 | 2.30 | veto (LF<3): same wall |

Deferral entry: Dockerfile + compose **deferred until** a deployment need exists AND a container runtime is available
(production-like tier may stay host-based; `deployment.md` will say so).

## 11. CI

| Option | IR | TN | P | LF | CX | MT | RR | Weighted | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **GitHub Actions** | 5 | 4 | 4 | 5 | 5 | 5 | 4 | **4.60** | **chosen** — seeded at `.github/workflows/`; covered by ADR [0004](../decisions/0004-language-and-toolchain.md) |
| Self-hosted / other CI | 2 | 2 | 3 | 3 | 2 | 3 | 2 | 2.45 | veto (TN<3): no infrastructure to justify it |

## Decision summary

| # | Choice | Decision | Record |
|---|---|---|---|
| 1 | Language / toolchain | Python 3.14 + uv + pytest/ruff/mypy + GitHub Actions; C++/Rust deferred (measured trigger) | ADR 0004 |
| 2 | API | FastAPI + Pydantic, HTTP+JSON, OpenAPI | ADR 0009 |
| 3 | Jobs | Postgres-backed thin queue + worker pool (built here) | ADR 0005 |
| 4 | Metadata store | PostgreSQL | ADR 0005 |
| 5 | Storage/formats | Local FS content-addressed; MCAP + LeRobot v3 + Parquet/Arrow | ADR 0006 |
| 6 | Versioning/lineage | Content hashes + manifests in catalog (build-thin) | ADR 0007 |
| 7 | Experiment tracking | Run records in catalog (build-thin); MLflow deferred (trigger) | ADR 0007 |
| 8 | Observability | Stdlib JSON logs + pynvml + file metrics; OTel/Prometheus/Grafana deferred (trigger) | ADR 0008 |
| 9 | Frontend | Deferred until phase 12 (scope fixed in phase 03) | deferral entry §9 |
| 10 | Containers | None now; Dockerfile deferred (trigger) | deferral entry §10 |
| 11 | CI | GitHub Actions (seeded) | ADR 0004 |

Every row above has an ADR or an explicit deferral entry with a trigger, satisfying the phase-02 acceptance criterion.
