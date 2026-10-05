# EXP-0016 — Concurrent operator workflows: does sharing the catalog cost anything?

- **Date:** 2026-10-04
- **Commit:** `bab9ab5` (clean tree)
- **Hardware:** Windows 11, x86_64, PostgreSQL 17.11 on 127.0.0.1:55432
- **Tooling:** `scripts/multi_workflow.py`
- **Method:** [methodology](../benchmarking/methodology.md). **One variable changes at a time** (rule 5), which is why
  this is a set of isolation legs plus one combined leg rather than a single "under load" number. 45 s window per leg,
  warm-up discarded, steady-state rather than burst.

## What this measures and why

The existing scaling campaign measures workers against workers: N ingest jobs, one worker versus two, wall-clock
speedup. That is the right question for a batch system and the wrong one for this product, because **an operator does
not run one workflow at a time.** On a real machine the catalog is being read by a browser polling six routes, written
by somebody curating task strings, drained by a worker doing ingest, and ticked by the monitor — all at once, against
one Postgres.

Four actors were run, each alone and then together, on a 1,000-episode catalog:

| Leg | Actor | What it does |
|---|---|---|
| `ingest` | 2 real worker processes | drain a steady stream of real `ingest_source` jobs over real MCAP bags |
| `read` | 1 API process, 1 client | poll the six routes the operator's pages use, every 3 s |
| `curate` | 1 API process, 1 client | create an entry, map a string, dismiss a string — 3 round trips per action |
| `monitor` | 1 API process | `POST /api/v1/monitoring/tick` every 5 s |

The ingest leg is deliberately **steady state**: it keeps a backlog of `workers × 2` jobs and tops up by exactly the
deficit, because an operator does not hand over twelve jobs and go away. An earlier burst design drained in nine
seconds and measured nothing.

## Result

Isolation legs (45 s each, no other actor running):

| Leg | Jobs done | Throughput | Sampler | p50 | p95 | max |
|---|---:|---:|---|---:|---:|---:|
| `ingest` | 160 | 3.54 jobs/s | — | — | — | — |
| `read` | — | — | 15 requests | 79.8 ms | 169.6 ms | 210.2 ms |
| `curate` | — | — | 480 requests | 66.5 ms | 226.6 ms | 274.6 ms |
| `monitor` | — | — | 9 ticks | **406.6 ms** | 504.8 ms | 504.8 ms |

Pairwise and combined:

| Leg | Jobs done | Throughput | Sampler | p50 | p95 | max | Errors |
|---|---:|---:|---|---:|---:|---:|---|
| `ingest + read` | 144 | 3.19 jobs/s | read | 94.7 ms | 181.1 ms | 182.0 ms | 0 |
| `ingest + curate` | 163 | 3.58 jobs/s | curate | 63.5 ms | 153.7 ms | 261.3 ms | 0 |
| **`ingest + read + curate + monitor`** | 162 | **3.58 jobs/s** | read | 55.3 ms | 203.1 ms | 249.9 ms | 0 |
| | | | curate | 63.2 ms | 158.0 ms | 261.0 ms | 0 |
| | | | monitor | 348.9 ms | 403.1 ms | 403.1 ms | 0 |

## Findings

**Contention is not the problem. This is a genuinely good result and it should be stated plainly.**

Ingest throughput with everything else running (3.58 jobs/s) is **within 1 % of throughput with nothing else running**
(3.54 jobs/s). Read latency alone (79.8 ms p50) and read latency with four actors competing (55.3 ms p50) are
indistinguishable — the combined figure is *lower*, because the 15-sample p50 on a 3 s poll interval is dominated by
which path in the rotation landed where. Curate latency is flat: 66.5 ms alone, 63.2 ms under full contention. Across
every leg: **zero errors, zero 5xx, zero stalled jobs, zero vocabulary writes lost.** The per-string advisory locking
added in ADR 0029 held under this load.

A single-node Postgres with a thin queue absorbs one operator plus a background worker comfortably. Nothing in this
record suggests adding workers, sharding, or a queue broker.

**The monitor tick is 5× the cost of an operator page read, and it is the most expensive single operation measured.**
`POST /api/v1/monitoring/tick` is **406.6 ms p50 alone, 348.9 ms under full load**, against a 79.8 ms page read. It
reads 26 features, maintains per-scope EWMA and median-MAD control limits over the metrics sink, evaluates eleven
rules, and persists incidents — every tick, forever, whether or not anything is wrong. EXP-0003 measured the *rule
evaluation* at 0.2 ms; the remaining ~406 ms is feature building and reading the sink.

This matters less than it looks, for a reason found while measuring it:

> **The monitor is never scheduled.** `MonitorService.tick()` has exactly two callers in the codebase: the HTTP route
> at `api/app.py:1829`, and the test suite. The worker loop (`cli.py::_worker_loop`) reaps deadlines and samples host
> gauges but never ticks the monitor; the FastAPI `lifespan` hook only initialises the schema. The route's own
> docstring says "The loop is a scheduler's job, not a webhook's" — and the loop was never written.

So the eleven rules, the control limits, the incident queue, the severity gates, the alert budget, the notifier and
the whole `/ui/incidents` page exist but **never execute in a running deployment**. The page measured 208 ms p95 and
renders zero rows, and it will render zero rows forever, because nothing has ever raised an incident. The 406 ms tick
is a cost that is not currently being paid; the cost actually being paid is that the monitoring product does not
monitor.

## Caveats

- 1,000 episodes, 2 workers, one machine, 45 s windows, 15–480 samples per leg. The read and monitor legs have
  small sample counts (15 and 9) because they poll on a 3 s and 5 s interval; their p95 is indicative, not precise.
- Ingest fixtures are a mix of the repo's generated MCAP bags (0.04–20 MiB), not a uniform size distribution, so
  "jobs/s" is a throughput of *these* jobs, not of episodes per second.
- No lock contention was induced at the vocabulary level beyond the curator's own loop. The ADR 0029 advisory-lock
  races are covered by `tests/integration/test_vocabulary.py::TestConcurrentWriters`, not by this campaign.
- Nothing here exercises a database restart, a disk-full condition, or a worker kill. EXP-0011 and EXP-0013 cover
  those.

## Reproduction

```bash
uv run python scripts/multi_workflow.py --episodes 1000 --jobs 12 --workers 2 --seconds 45
```

Seven legs, roughly 6 minutes.