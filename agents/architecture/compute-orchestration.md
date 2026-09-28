# Compute and Orchestration

Decisions: ADR [0005](../decisions/0005-catalog-and-job-queue.md) (thin Postgres queue + worker pool),
[0010](../decisions/0010-modular-monolith-and-workers.md) (monolith + out-of-process workers). State machine:
[data-flow.md](data-flow.md) §4. This is deliberately **not** a general-purpose scheduler (matrix §3 vetoes).

## 1. Queueing and dispatch

- Jobs live in `jobs` (catalog). Enqueue = transactional insert (idempotent via `idempotency_keys`).
- Dispatch: workers claim with `SELECT … WHERE state='queued' … ORDER BY priority, created_at FOR UPDATE SKIP LOCKED
  LIMIT 1`, set `state='running'`, `worker_id`, `lease_expires_at`. No central broker; every claim is atomic.
- **Priorities:** integer `priority` (0 = default, higher first). FIFO within a priority class by `created_at, id`.
  Two reserved classes: interactive (API-submitted, user-facing) vs batch (chained stages, GC). Starvation avoided by
  age-based priority boost after a configurable wait (documented default; measured later).
- **Chaining:** `ingest → validate → index` are separate jobs linked by `parent_job_id`; the next stage is enqueued on
  the parent's success (partial-pipeline failure semantics — each stage retries independently).

## 2. Worker pool and resource accounting

- Pool = N worker **processes** (default: `min(8, cpu_cores/2)`), started via CLI (`de worker`) or the API process's
  supervised pool in dev. Process-level parallelism sidesteps the GIL for CPU stages (ADR 0004).
- **Resource slots** (lease rows in catalog, so accounting survives restarts):
  - `cpu_slots` = worker count — ingest/validate/index/build/`workload(cpu)`.
  - `gpu_slots` = 1 on the dev box (RTX 5060 8 GB, environment.md) — `workload(gpu_*)` jobs acquire the GPU lease.
  - `mem_budget`: advisory guard — workers report RSS; a job exceeding its declared budget is flagged (soft limit),
    then failed on hard OOM (failure mode).
- **GPU allocation, including the no-GPU path:** workloads declare `resource: cpu | gpu_optional | gpu_required`
  ([api.md](api.md) workload interface). Admission at enqueue: `gpu_required` + no GPU visible → job rejected with
  `GPU_UNAVAILABLE` (never queued forever); `gpu_optional` falls back per its contract and records the actual device in
  the run record. Telemetry degrades identically (ADR 0008).
- Oversubscription is not attempted; 8 GB VRAM means one GPU workload at a time (measured revisits land in
  `agents/experiments/`).

## 3. Retries, timeouts, cancellation

| Concern | Policy |
|---|---|
| Retry | Bounded attempts per job type (default 3; `build`/`workload` default 2). Backoff: `min(cap, base·2^attempt) + jitter`, base 2 s, cap 5 min. Retriable vs terminal classified by reason code (`RETRYABLE_IO`, `VALIDATION_PROFILE_INVALID` = terminal). |
| Timeout | Per-type default wall-clock (ingest 10 min, validate 5 min, index 10 min, build 30 min, workload per spec) + per-job `deadline` override. Watchdog in worker + dispatcher sweep for stuck leases. Late completions of timed-out jobs are discarded safely (idempotent handlers, content-addressed outputs). |
| Cancellation | Cooperative: `cancel_requested` state + `cancel_token` checked between work units (e.g., between episodes). Grace period (default 10 s) then force process kill for that job. Queued jobs cancel instantly. Race "completed before cancel landed" resolves to `succeeded` with a recorded transition (documented in the state machine). |
| Heartbeats | Worker updates `lease_expires_at` every 5 s (lease 30 s). Expired lease → job back to `queued`, `attempts+1`, transition logged (`REASON_WORKER_LOST`). |
| Duplicate execution | Possible by design (at-least-once). Handlers are idempotent: ingest dedupes on `source_hash`; index upserts on `(episode_id, extractor_version)`; build publishes only on manifest-hash match. |

## 4. Delivery semantics

**At-least-once with idempotent handlers** (ADR 0005). Exactly-once is explicitly *not* claimed anywhere in the API or
docs; the reproducibility contract (manifest hashes, content addressing) makes repeats harmless and observable. Every
duplicate run shows up in `job_transitions` and metrics (`jobs.duplicate_executions`).

## 5. Observability hooks

- Per-job: `job_id`/`correlation_id` on every log line; `jobs.queue_time`, `jobs.run_time`, `jobs.attempts`,
  `jobs.retries`, `jobs.timeouts`, `jobs.canceled`, `workers.heartbeat_age`, `workers.cpu_slots_free`,
  `workers.gpu_slots_free` metric points (schema: [../benchmarking/metric-schema.md](../benchmarking/metric-schema.md)).
- Scheduler health: `GET /api/v1/health` reports queue depth, oldest queued age, worker lease summary (platform
  engineer's first stop; FR-014/FR-015).

## 6. What this deliberately is not

- No fair-share multi-tenancy, no gang scheduling, no distributed placement — single machine, single user (ADR 0003
  scope). Triggers to revisit (multi-node, multi-tenant) live in
  [technology-decision-matrix.md](technology-decision-matrix.md) §3 and require an ADR superseding 0005.
