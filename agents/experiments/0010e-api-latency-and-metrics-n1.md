# EXP-0010e: HTTP read latency at 10 rps (NFR-010) — and the metrics-read N+1 it exposed

- **Date (UTC):** measured 2026-10-03
- **Author/agent:** benchmark-engineer
- **Status:** done
- **Related ADR / requirement:** NFR-010 (API latency p95 ≤ 300 ms for CRUD/list endpoints at 10 rps, [requirements.md](../spec/requirements.md) §2); stage-4 criterion "NFR targets met or explicitly revised with evidence" and "Bottlenecks identified by profiling … each addressed via an experiment record".
- **Tool:** `scripts/scale_campaign.py --leg api --seconds 60` (10 rps round-robin over 6 endpoints) against a live `de api` server (uvicorn, 1 worker) on `127.0.0.1:8123`, with `DE_DATABASE_URL` pointed at a throwaway catalog seeded with 10,000 episodes + 20 jobs (so the list endpoints page over real rows rather than an empty table).

## Hypothesis / purpose

NFR-010 is the last unmeasured NFR of the stage-4 set. The question is simple: does the CRUD/list contract answer within 300 ms p95 at a modest 10 rps over a realistically populated catalog? The campaign run answers it — and the first run **failed** on one endpoint, which is the more interesting half of this record.

## Configuration / provenance

| Field | Value |
|---|---|
| git commit / dirty | `6a20d7d` / dirty |
| OS / Python / app | Windows 11 10.0.26200 / 3.14.5 / 0.1.0 |
| hardware | Intel Family 6 Model 198, 24 logical CPUs, 33.75 GB RAM, disk `C:\\` |
| server | `de api` (uvicorn), `DE_API_PORT=8123`, one process |
| catalog | throwaway `data_engine_scale_api_<hex>` (PG 17.11), seeded 10k episodes + 20 jobs via `COPY` |
| load | 10 rps round-robin over `/api/v1/health`, `/api/v1/episodes?limit=50`, `/api/v1/jobs?limit=50`, `/api/v1/failures`, `/api/v1/incidents?limit=50`, `/api/v1/metrics`; 60 s; ~61–92 samples per path |
| target | p95 ≤ 300 ms per path, no 5xx |

## Results

**Summary:** five of the six reads passed the first run; the metrics read missed the 300 ms target at p95 591 ms. One query-loop fix later every read passes at p95 between 29 ms and 224 ms with no 5xx anywhere, and the two tables below name the six routes exactly as measured.

### Run 1 — before the fix: NFR-010 **missed**

| Path | n | p50 | p95 | statuses | verdict |
|---|---|---|---|---|---|
| /api/v1/health | 62 | 15.4 ms | 30.2 ms | 200 | pass |
| /api/v1/episodes?limit=50 | 62 | 78.2 ms | 110.3 ms | 200 | pass |
| /api/v1/jobs?limit=50 | 62 | 61.7 ms | 166.9 ms | 200 | pass |
| /api/v1/failures | 62 | 66.7 ms | 170.2 ms | 200 | pass |
| /api/v1/incidents?limit=50 | 61 | 61.9 ms | 152.4 ms | 200 | pass |
| **/api/v1/metrics** | 61 | **459.4 ms** | **590.6 ms** | 200 | **FAIL (target 300 ms)** |

### Attribution: 8 round trips where 1 would do

Standalone timing of the endpoint's own work against the same catalog: **8 × `count_jobs` = 588 ms of the 599 ms total** (98%). `_metrics_model` computed `jobs_queue_depth` as `{state.value: catalog.count_jobs(state) for state in JobState}` — 8 states, 8 separate `SELECT count(*)`, each opening its own connection. The metrics-sink read (7 ms, 1,497 records) and the aggregation (`summarize` + `series` + heartbeat, 3.8 ms) were noise. `_status_model` had the identical fan-out.

**Fix** (one commit-sized change): `PostgresCatalog.count_jobs_by_state()` — a single `SELECT state, count(*) FROM jobs GROUP BY state`, zero-filling absent states — replaces the fan-out in both `_status_model` and `_metrics_model` ([repository.py](../../src/data_engine/catalog/repository.py), [app.py](../../src/data_engine/api/app.py)). Same numbers, one scan.

### Run 2 — after the fix: NFR-010 **met**

| Path | n | p50 | p95 | statuses | verdict |
|---|---|---|---|---|---|
| /api/v1/health | 92 | 19.9 ms | 29.3 ms | 200 | pass |
| /api/v1/episodes?limit=50 | 91 | 78.2 ms | 190.3 ms | 200 | pass |
| /api/v1/jobs?limit=50 | 91 | 65.3 ms | 176.8 ms | 200 | pass |
| /api/v1/failures | 91 | 62.7 ms | 159.7 ms | 200 | pass |
| /api/v1/incidents?limit=50 | 91 | 55.3 ms | 155.4 ms | 200 | pass |
| /api/v1/metrics | 91 | **118.9 ms** | **223.8 ms** | 200 | **pass** |

`/api/v1/metrics`: p50 459 → 119 ms (−74%), p95 591 → 224 ms (−62%). Every path ≤ 300 ms p95, no 5xx anywhere: **NFR-010 met**.

## Honest caveat

- One uvicorn process, one machine, localhost. The numbers are the local-tier claim NFR-010 makes ("modest load"), not a capacity ceiling. The 10 rps is round-robin across 6 paths, so each path sees ~1.7 rps; the endpoint cost is the per-request cost, not a concurrency-stress figure.
- `/api/v1/metrics` still reads and aggregates the whole (bounded) metrics sink per request: 200k-record cap, tail-read in 1 MiB chunks (EXP-0007's fix). At ~1,500 records that is ~7 ms; at the 200k cap the read grows. If the sink ever runs long enough to sit at the cap, this endpoint is the first to re-breach and should be re-measured.
- The list endpoints' p95 (110–190 ms) is dominated by the per-request connection setup (`connect()` opens a fresh connection per repository call), not by query time — the same pattern the metrics N+1 rode in on. A connection pool would compress every path; deliberately not done here (no ADR, no evidence it is needed at this load).

## Trade-offs / verdict

**NFR-010 met**, and the campaign earned its keep twice: it found a real N+1 (8 counts per request) that unit tests could not see, and the fix is provably the same numbers in one query. This is the third quadratic/n+1 defect found by measuring at scale rather than reasoning about it (after EXP-0005's path-string rebuild and EXP-0010c's quality-summary assembly).

**Decision: record + fix adopted.** The fix is `count_jobs_by_state()` on the repository, used by both status and metrics models.

## Follow-ups

- If the metrics sink can reach its 200k-record read cap in practice, re-measure `/api/v1/metrics` and consider caching the aggregate across requests.
- A pooled connection layer would compress all six paths (the per-request connect is now the dominant cost on the list endpoints); revisit under an ADR if load increases.
