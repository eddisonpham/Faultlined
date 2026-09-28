# 0005. Catalog and job execution: PostgreSQL + thin job queue + worker pool

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** architect agent (phase 02), per owner-approved ADR 0003

## Context

Requirements FR-009/FR-010 demand a full job lifecycle (submit, queue, run, bounded retry with backoff, timeout, cancel)
that survives restarts, plus a catalog with transactional lineage (FR-004, FR-008). Hardware is one machine, one GPU
([../spec/environment.md](../spec/environment.md)); no Docker/K8s. The job lifecycle *is* part of the product
(spec §4) and must be directly testable. OSMO (#14, #31) shows the industry pattern (declarative tasks, task-I/O
wiring) but runs on Kubernetes, which we cannot verify locally. Scored matrix: [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §3, §4.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **PostgreSQL catalog + thin queue + worker pool (chosen)** | One transactional store for catalog + job state; `SELECT … FOR UPDATE SKIP LOCKED` dispatch; testable semantics; zero brokers; matches "build-thin" mandate | We own the (small) scheduler semantics; Postgres is one more local service to run (or embed via service-less install) |
| Celery/RQ + Redis | Mature worker ecosystem | Opaque retry/cancel semantics; extra broker process; we'd rebuild visibility on top anyway |
| Ray | Distributed compute; great resume keyword | Solves cluster problems we don't have; heavyweight runtime; would outsource the deliverable subsystem |
| Kubernetes + Argo/Kueue | Industry-standard orchestration (#14, #31) | No container runtime locally; cannot test; pure keyword stuffing at our scale |
| Temporal | Durable execution, strong retry semantics | Server + SDK determinism rules; heavier than the workflow graph we run |
| SQLite catalog | Zero-config | Single-writer bottleneck with API + N workers; weaker concurrent guarantees |

## Decision

A **PostgreSQL** database is the system of record: episodes, metadata index, validation results, datasets, builds,
lineage edges, jobs, runs. Job dispatch uses a **thin queue** in Postgres: jobs table with state machine
(`queued → running → succeeded | failed | canceled | timed_out`, `running → retrying → queued`), atomic claim via
`FOR UPDATE SKIP LOCKED`, exponential backoff with jitter and bounded attempts, per-job timeouts, cancellation as a
cooperative flag + final state transition, and worker heartbeats with stale-claim requeue. Worker pool runs as local
processes (process-parallel for CPU stages; GPU workloads serialized by resource accounting — see `compute-orchestration.md`).

**Delivery stance:** **at-least-once** execution with idempotent stage handlers (idempotency keys on submission,
content-addressed artifacts make repeats harmless); exactly-once is explicitly *not* claimed. Restart recovery: jobs in
`running` with stale heartbeats return to `queued` (NFR-006).

**Deferred with trigger:** multi-node scheduling (Ray/K8s-style) is deferred until there is more than one machine
(ADR 0003 consequences; technology-matrix triggers).

## Consequences

- (+) Job semantics are code we can unit- and failure-test directly (FR-009, failure-mode catalog).
- (+) Catalog + queue share transactions — a build and its lineage commit atomically.
- (−) At-least-once means handlers must be idempotent; tests must cover duplicate execution (failure-mode catalog).
- (−) Postgres must be installed locally (production-like tier may run it as a Windows service); documented in
  `deployment.md` (phase 03).

## Docs updated

- [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §3, §4
- [../research/technology-matrix.md](../research/technology-matrix.md) (Postgres/queue core; Ray/K8s/Argo/Temporal/Celery excluded)
- [../spec/requirements.md](../spec/requirements.md) §3 (failure-mode stance)
