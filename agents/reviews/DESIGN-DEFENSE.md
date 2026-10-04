# DESIGN-DEFENSE: Faultlined (robot episode data engine)

- **Date (UTC):** 2026-10-04
- **Reviewer:** Buffy (Freebuff)
- **Scope:** stage 5 resume/Demo-Ready deliverables — README + architecture diagram, quickstart, demo script, experiment-trace metrics summary, design-defense record, final repo-wide review.
- **Link:** [experiment registry](../experiments/registry.md), [HANDOFF.md](../../agents/HANDOFF.md)

## 1. What is defended

Faultlined is a single-machine, local-first robot episode data engine: it ingests raw recordings (MCAP logs, LeRobot v2/v3 dataset directories), validates them against declared schemas and quality rules, indexes episodes into a PostgreSQL catalog with Parquet frame metadata, assembles curated content-addressed dataset builds, and serves a deterministic fault monitor and a reproduction-grade benchmark harness.

The non-negotiable for this project is the last sentence of the problem statement: **the platform is the product; models are workloads**. There is no model in the API, no model in the catalog, no model in the UI. Workloads run behind a narrow runner interface; inference/HF datasets are inputs, not deliverables.

## 2. Design decisions and the evidence behind them

### 2.1 Why Postgres for the catalog *and* the queue (not Redis/Kafka/Ray)

- The queue is a small, well-understood tool: **at-least-once with idempotent handlers**, ordered claims, bounded retries. Installing a broker adds a deployment tier, a connection pool, and a failure mode that this single-machine design never asked for.
- Postgres gives the queue AND the catalog a single, transactional source of truth. A job row and the catalog rows it writes are reachable from the same `SELECT`.
- NFR-008 measured at 10k episodes/500 G raw: read latency, ingest linearity, worker-count scaling, and queue latency all fit without architectural change. A dedicated broker would have bought nothing at that scale but a hop.
- The CADC/v0 design was rejected in ADR 0005: the trigger to revisit is >3 communicating services or a real distributed-queue requirement. Both are absent.

### 2.2 Why a custom queue instead of an off-the-shelf scheduler

- The lifecycle is small: enqueue (idempotent), atomic claim, priority FIFO, bounded retry, deadline sweep, cooperative cancel, heartbeat/stale-claim requeue. That is a handful of SQL statements, not a distributed-system dissertation.
- The scheduling problem we actually have is “one machine, one worker” — a slot pool, not a scheduler. Concurrency is obtained by running N workers, which is trivially correct because the queue serialises claims.
- The recorded trigger for revisiting is a multi-node requirement or a real-time control plane — neither is on this roadmap.

### 2.3 Why content addressing over a blob store (ADR 0006)

- The artifact is a working dataset, not a binary to dual-publish. A local, immutable, hash-named tree is a complete CDN for this scope.
- Content addressing makes replay, rebuild, and restore trivially correct: the hash is the address, the manifest is the proof, and the artifact tree never needs a GC pass beyond “delete files older than N days” (F15).
- S3/MinIO was rejected: 228 GB of local NVMe suffices; adding an object tier is an explicit deferred extension (NFR-006 follow-up is artifact *durability*, not *motion*).

### 2.4 Why deterministic rules, not a model, for the fault monitor (ADR 0020)

- The monitor's job is *detect and contain*, not generate. A deterministic notifier with per-scope EWMA/median-MAD control limits over a versioned 26-feature vector was measured at **P50 0.2 ms / P95 0.3 ms** per tick (EXP-0003) — five orders of magnitude inside budget.
- Learned detection was explicitly rejected: no real labels, an ineffective sample size, and a non-reproducible output for a system whose value is being trusted. The alert budget, not compute, is the binding constraint.
- Run records keep the benchmark reproducible: same selection + same policy + same commit => identical build hash, asserted as a hard gate (NFR-004).

### 2.5 Why host-based development, containers optional (ADR 0012)

- The captured hardware has no Docker socket, no `g++`, and a small 8 GB GPU. A container would only add image size and a syscall-era mismatch on Windows.
- The production-like local tier is specified to the OS-scheduler level: `just api` under Task Scheduler with “restart on failure”, `just worker` beside it, backup via `pg_dump`, GC/manual. Saying what is *not* automated is as deliberate as saying what is.

### 2.6 Why stdlib JSON logs + file metrics instead of OTel/Prometheus (ADR 0008)

- Correlation IDs across API -> worker -> logs already give cross-component tracing for this single-host topology.
- The metric registry is emitted at call sites and aggregated on demand; JSONL is the source of truth for `GET /api/v1/metrics` and `/ui/metrics`.
- OpenTelemetry/Prometheus/Grafana are deferred to the recorded triggers (>=3 independently communicating services, or a scrape/alert/dashboard need). No speculative telemetry service.

### 2.7 Why metrics never break the pipeline (ADR 0008 / F13)

- A sink that raises is logged once and swallowed; a recorder with no sink is a no-op. The worker loop samples host telemetry every 60 s and drops unreadable fields — it never raises so a full disk or a missing GPU probe cannot kill a job.
- NFR-006 worker-crash half is verified: real hard kills mid-ingest recovered via the production reaper to exactly one episode + one intact artifact (EXP-0011 + EXP-0013).

### 2.8 What was deliberately *not* built

- GPU workload execution (no consumer exists; `gpu_required` admission is unstubbed).
- Formal database migrations (schema is idempotent DDL; ADR 0028 documents `de migrate`).
- Retry backoff, worker leases, mid-call cancellation, orphan GC, workload execution, Parquet metadata indexing.
- Multi-user auth/RBAC, cloud/distributed deployment, S3 tier, embedded-model path.
- Quantization/TensorRT/ONNX/edge optimization — explicitly out of scope and never the central contribution.

## 3. Resume metrics traced to experiments

| Measure | Value | Evidence |
|---|---|---|
| Unit/contract/integration/e2e tests (default gate, `just ci`) | 1,357 passed, 0 failed | [HANDOFF §1](#1-handoff) |
| Coverage floor | 91.46% (gate 70%) | [definition-of-done §1](#1-scaffolding) |
| Strict mypy | clean (76 source + 22 experiment modules) | [HANDOFF §1](#1-handoff) |
| UI audit (`just ui-audit`) | 60 page loads across 4 themes clean | [EXP-0008](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |
| Synthetic ingest microbenchmark P50 | 0.6084 ms (10 trials, 0 failures, committed baseline) | [EXP-0001](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |
| MCAP ingest P50 | 6.21 MiB/s (0/5 failures; 10.8–18.3 MiB/s after EXP-0005 flatten plan) | [EXP-0004](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |
| Worker-count scaling | 2.35× wall reduction on 8 jobs, 0 failures | [EXP-0010b](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |
| Catalog scale NFR-003 | all query p95 ≤ 200 ms at 10k episodes | [EXP-0010c](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |
| API latency NFR-010 | all 6 endpoints ≤ 300 ms p95 at 10 rps | [EXP-0010e](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |
| Worker-kill crash recovery | 1 episode + 1 intact blob, no duplication, no corruption | [EXP-0011](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |
| F6 deadline watchdog (mid-handler) | `TIMED_OUT` at checkpoint + post-handler re-check, no handler-failure pollution | [EXP-0012](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |
| F9/F10/F12/F13/F15 measured coverage | F9 `IO_DISK_FULL`, F10 503+Retry-After + worker survives, F12 CPU-only by construction, F13 sink failures swallowed, F15 atomic publish | [EXP-0013](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |
| Restore drill (`scripts/fault_drill.py`) | executed and converged | [HANDOFF §1](#1-handoff) |
| Observer NOT included | no OpenTelemetry/Prometheus/Grafana/LLM in detection path | [ADR 0008](#22-why-a-model-not-a-model-for-the-fault-monitor-adr-0020) |

## 4. Risks and open questions

1. **Commit-based baseline is a regression tripwire, not a target.** Synthetic ingest P50 0.6084 ms; run-to-run spread ≈24%. The harness refuses to compare off-host. Citable as measured, not as a production claim.
2. **Artifact publication and catalog registration are separate transactions.** Worker crash recovery, leases and orphan GC are future work (NFR-006 remainder; F15 collector is manual).
3. **No detection-accuracy figure for the notifier.** The full fault-injection campaign (backlog B-016) is still open; precision/recall is not claimed anywhere.
4. **GPU OOM cannot be injected.** The engine runs no GPU workloads, so there is no driver to OOM; NFR-007 is met by construction (nullable telemetry, best-effort GPU probe).
5. **The owner's system PostgreSQL on 5432 is unverified**; `just pg-up` covers development with an isolated cluster on 55432.
6. **No hosted runner.** `.github/workflows/` was removed on 2026-09-30; the guarantee is whoever runs `just ci` before committing, reinforced by pre-commit hooks (lint/types/hygiene on changed files only).

## 5. Final repo-wide review verdict

- **Verdict:** accept with recorded follow-ups (all in this document or in the linked records; nothing open that blocks the Resume/Demo-Ready stage).
- **Clean history:** this commit is the stage-5 cleanup commit; EXP-0012/EXP-0013 and the experiment registry are committed alongside it.
- **Hygiene (`scripts/check_repo_hygiene.py`):** OK — structure, Markdown links, ADRs, secret patterns.
- **Gates:** `just ci` green (1,357 passed, 91.46% coverage, strict mypy clean, hygiene OK, OpenAPI contract pass); `just ui-audit` clean at 60 page loads across 4 themes.
- **Obligations that remain after this commit:** F9/F10 live-drill runbooks (eligible once a quota-ed mount/real isolated Postgres is available), F12 GPU workload consumer, F15 collector documentation — all flagged in [HANDOFF §7](#7-next-steps-stage-5--production-hardening).
