# EXP-0010b: Worker-count scaling on real Postgres — 1 vs 2 workers draining 8 ingest jobs

- **Date (UTC):** measured 2026-10-02
- **Author/agent:** benchmark-engineer
- **Status:** done
- **Related ADR / requirement:** NFR-008 ("more workers divide the work" — 10k-episode / ~500 GB working set handled without architectural change); stage-4 criterion "scaling curves measured (worker slots, data size, concurrency)"; [EXP-0010a](../experiments/0010a-data-size-scaling-curve.md) (ingest linearity) and [EXP-0010c](../experiments/0010c-catalog-scale-nfr003.md) (10k catalog read + validate) are the other two legs of the EXP-0010 scaling triangle.
- **Tool:** `scripts/scale_campaign.py --leg job-scaling` against a throwaway Postgres DB (real `data_engine` user, real schema, real worker processes via `multiprocessing`). 8 `ingest_source` jobs for the 120 s MCAP fixture (`var/real-data/so101_pick_place_120s.mcap`), drained by 1 worker vs 2 workers. Each job is a real MCAP ingest through the full path: claim → MCAP read → artifact hash → episode register → quality compute → validation record.
- **Schema note:** same as EXP-0010c — the dev catalog `episodes` table has no `source` column; the `ingest_source` job payload carries `{"source": "<path>"}` as a *payload* field (the path the MCAP reader opens), not a catalog filter. The catalog identity is `(source_hash, episode_key)`.

## Hypothesis / purpose

NFR-008's "more workers divide the work" claim is the concurrency half of the 10k-episode proposition. The ingest side is the relevant shape: a batch of episodes lands as a queue of `ingest_source` jobs, and adding worker processes should divide wall-clock time roughly in proportion to the worker count, modulo queue claim overhead and the `FOR UPDATE SKIP LOCKED` claim path. The question:

1. Does 2 workers give ~2× speedup on a real 8-job batch, or does some shared resource (disk, DB writes, artifact hash) saturate and bend the curve?

## Configuration / provenance

| Field | Value |
|---|---|
| git commit / dirty | `6a20d7d` / dirty |
| OS / Python / app | Windows 11 10.0.26200 / 3.14.5 / 0.1.0 |
| hardware | Intel Family 6 Model 198, 24 logical CPUs, 33.75 GB RAM, disk `C:\\` |
| database | throwaway `data_engine_scale_<hex>` on `postgresql://data_engine@127.0.0.1:55432` (PG 17.11) |
| fixture | `var/real-data/so101_pick_place_120s.mcap` (0.83 MiB, 14,520 messages, 5 channels) |
| workload | 8 `ingest_source` jobs, same fixture, distinct idempotency keys per job |
| workers | 1 process vs 2 processes, each a real `IngestWorker` claiming from the same queue via `FOR UPDATE SKIP LOCKED` |
| measurement | wall clock from enqueue to all-8-terminal, per worker count; throughput = 8 / wall |

## Results

| Workers | Wall (s) | Throughput (jobs/s) | Failed |
|---|---|---|---|
| 1 | 3.912 | 2.045 | 0 |
| 2 | 1.665 | 4.804 | 0 |

**Speedup 1→2: 2.35×**

2 workers are *superlinear* vs 1 worker on this batch (2.35× vs the 2.0× ideal). The most likely cause is per-job fixed overhead (claim transaction, artifact `INSERT ... ON CONFLICT`, episode `INSERT ... ON CONFLICT`, quality insert, validation record insert) amortized over fewer wall-clock seconds when two workers overlap that overhead with each other's I/O. On a larger batch (the 10k validate job in EXP-0010c is the write-side analogue) the fixed overhead matters less and the curve should bend toward linear; this 8-job measurement is the low-volume end of the curve and should not be read as a sustained throughput claim.

No job failed in either run. All 8 episodes ingested, hashed, registered, quality-computed, and validated in both configurations.

## Honest caveat

- 8 jobs is a small sample. The superlinear 2.35× is plausible but not statistically tight; the relevant NFR-008 claim is about the 10k-episode scale, not the 8-job micro-batch. EXP-0010c's validate job (10k episodes, single worker, 14.4 min) is the large end; a 2-worker validate run would be the direct concurrency measurement at scale and is the natural follow-up.
- The fixture is the 120 s bag (0.83 MiB). A longer bag would change the ingest/worker ratio and could surface disk or DB-write saturation that this tiny fixture does not. EXP-0010a already measured ingest linearity across bag sizes on the harness side; this leg is the queue+worker analogue on real Postgres with real MCAP bytes.

## Trade-offs / verdict

**NFR-008's concurrency half holds at the 8-job scale:** 2 workers divide the work with a 2.35× wall-clock reduction and no failures. Combined with EXP-0010a (ingest linear to the hour bag) and EXP-0010c (10k catalog reads within NFR-003 targets, validate completes in 14.4 min single-worker), the 10k-episode / ~500 GB proposition is supported on ingest throughput, ingest concurrency, catalog read latency, and validate throughput — all four directions measured, none showing an architectural cliff.

**Decision: record** (measurement record; no adopt/reject object). No code change.

## Follow-ups

- 2-worker validate over the 10k seeded catalog (the direct large-scale concurrency measurement NFR-008 actually claims). The seed + validate path in `scripts/scale_campaign.py --leg catalog-scale` already does single-worker validate; a 2-worker variant is the missing data point.
- Larger ingest batch (e.g. 64 jobs) to see whether the 2.35× holds or bends toward 2.0× as fixed overhead amortizes.
