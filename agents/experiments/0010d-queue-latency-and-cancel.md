# EXP-0010d: Queue latency at depth 100 and cancel effect — NFR-005, decomposed

- **Date (UTC):** measured 2026-10-02 (two runs; the cancel leg numbers below are the 2026-10-02 21:1x run)
- **Author/agent:** benchmark-engineer
- **Status:** done
- **Related ADR / requirement:** NFR-005 ("Enqueue→start p95 ≤ 1 s at 100 queued jobs; cancel takes effect ≤ 2 s", [requirements.md](../spec/requirements.md) §2); [ADR 0015](../decisions/0015-cooperative-job-lifecycle.md) (cooperative cancellation); stage-4 criterion "scaling curves measured (… concurrency)".
- **Tool:** `scripts/scale_campaign.py --leg enqueue-start` and `--leg cancel`, throwaway Postgres DB, real `IngestWorker` processes, real `ingest_source` jobs over `var/real-data/so101_pick_place_120s.mcap` (~0.4 s service time per job).

## Hypothesis / purpose

NFR-005's two clauses each need decomposing before they can be measured honestly:

1. **"Enqueue→start p95 ≤ 1 s at 100 queued jobs"** is ambiguous about *whose* start. A job placed behind 100 others at ~0.4 s service time waits ~100 × 0.4 = 40 s as queue physics — no scheduler can make that ≤ 1 s. So the leg measures the two things ≤ 1 s can legitimately mean, and reports the raw queue wait beside them rather than hiding it:
   - (a) **submit latency at depth 100**: the enqueue itself (DB round trip + idempotency work) while 100 are already queued;
   - (b) **dispatch latency with a free worker**: submit → claim, when a worker is idle — pure machinery (submit + worker poll cadence + claim transaction), no queue wait.
2. **"Cancel takes effect ≤ 2 s"**: cancel a *queued* job and measure request → `canceled` state, with 60 real jobs ahead of it and a live worker draining the queue (so the cancellation is racing real claims, not idling).

## Configuration / provenance

| Field | Value |
|---|---|
| git commit / dirty | `6a20d7d` / dirty |
| OS / Python / app | Windows 11 10.0.26200 / 3.14.5 / 0.1.0 |
| hardware | Intel Family 6 Model 198, 24 logical CPUs, 33.75 GB RAM, disk `C:\\` |
| database | throwaway `data_engine_scale_<hex>` on `postgresql://data_engine@127.0.0.1:55432` (PG 17.11) |
| fixture | `var/real-data/so101_pick_place_120s.mcap` (0.83 MiB, 14,520 messages) |
| enqueue-start setup | 100-job backlog, 20 submit samples at depth 100, 20 dispatch samples with a free worker, 1 worker process |
| cancel setup | 60-job backlog + 10 measured queued jobs, 1 live worker, `request_cancel` per measured job |

## Results

### Enqueue→start decomposed (NFR-005a)

| Measurement | n | p50 | p95 | max | target |
|---|---|---|---|---|---|
| submit at depth 100 | 20 | 45.99 ms | 152.09 ms | — | ≤ 1 s |
| dispatch, free worker (submit → claim) | 20 | 0.68 s | 0.78 s | 0.79 s | ≤ 1 s |
| first claim after worker ready (100-deep queue) | 1 | 84.25 s | — | — | (physics, not a target) |
| raw queue-position wait (submitted at depth ~100) | 1 | 84.95 s | — | — | (physics, not a target) |

- **Dispatch p95 = 0.78 s ≤ 1 s: MET** under the only reading that is physically achievable. The 0.78 s is dominated by the worker's 0.25 s poll cadence and the claim transaction, not by queue depth.
- **Submit at depth 100 p95 = 152 ms**: enqueuing into a 100-deep queue costs the same as enqueuing into an empty one; the queue is not the bottleneck at this depth.
- **The raw 84–85 s waits are service-rate physics, not a regression**: ~100 jobs × ~0.84 s wall service per job under one worker. They are reported explicitly so nobody reads "enqueue→start" as if it were a scheduler number. NFR-005's wording is satisfied under the dispatch reading; under the raw queue-position reading it is unsatisfiable by any scheduler and should not be asserted.

### Cancel effect (NFR-005b)

| Measurement | n | canceled_ok | p50 | p95 | max | target |
|---|---|---|---|---|---|---|
| request → `canceled` (queued job, 60 ahead, live worker) | 10 | 10/10 | 0.073 s | 0.175 s | 0.175 s | ≤ 2 s |

**Cancel effect max = 0.175 s ≤ 2 s: MET**, with an order of magnitude of headroom. All 10 measured jobs canceled before a live worker could claim them; the cooperative path (`cancel_requested` → worker settles) is exercised by the unit/integration suite, not here — this leg measures the queued-job path where cancellation is a single state transition.

## Honest caveat

- The two legs use one worker and a ~0.4 s service job. A slower job (the hour bag is ~11 s) deepens the raw wait proportionally — the physics column would grow, the dispatch and cancel columns would not.
- `first_claim_after_worker_ready` at 84 s includes the worker's poll cadence once and then a full 100-job drain before the measured job's turn: it is a queue-depth fact, deliberately reported next to the ≤ 1 s numbers so the decomposition is visible in one table.

## Trade-offs / verdict

**NFR-005 met under its achievable reading:** dispatch (submit → claim with a free worker) p95 = 0.78 s ≤ 1 s, submit-at-depth p95 = 152 ms, cancel effect max = 0.175 s ≤ 2 s. The clause "enqueue→start p95 ≤ 1 s at 100 queued jobs" is **not** satisfiable as a raw queue-position bound (84 s at this service rate) and the record says so rather than relabeling the number; a wording revision (e.g. "dispatch p95 ≤ 1 s with an available worker; queue-position wait is service-rate-bounded and reported") is the honest fix and is left to the owner in the stage-4 NFR review.

**Decision: record** (measurement record; no adopt/reject object). No code change.

## Follow-ups

- NFR-005 wording revision in [requirements.md](../spec/requirements.md) §2 to separate dispatch latency from queue-position wait (stage-4 criterion "NFR targets met or **explicitly revised** with evidence").
- Multi-worker dispatch measurement: dispatch p95 under 2–4 workers should stay ≤ 1 s; the claim transaction is `FOR UPDATE SKIP LOCKED`, so the prediction is no depth penalty, but it is unmeasured.
