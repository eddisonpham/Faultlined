# EXP-0003: Deterministic notifier — evaluation and read latency

- **Status:** current
- **Date:** 2026-09-29
- **Question:** What does one evaluation tick of the notifier cost, and what does reading
  the incident queue cost?
- **Method:** `benchmarks/harness.py` workloads `monitor-evaluate` and `api-incidents-catalog`,
  each measured in its own process per [ADR 0019](../decisions/0019-benchmark-workload-isolation.md).
  `monitor-evaluate` warms every baseline scope first so the measurement covers the statistical
  path (median/MAD per scope), not the cold-start abstention path.
- **Related:** [ADR 0020](../decisions/0020-deterministic-monitoring-notifier.md),
  [automation plan](../implementation/automated-monitoring-plan.md), [EXP-0002](0002-run-intelligence-workloads.md).

## Results

| Workload | n | Warmups | p50 | p95 | p99 | Failures |
|---|---|---|---|---|---|---|
| `monitor-evaluate` | 50 | 5 | **0.2 ms** | 0.3 ms | 0.3 ms | 0 |
| `api-incidents-catalog` | 10 | 3 | **7.4 ms** | 9.8 ms | 9.8 ms | 0 |

`monitor-evaluate` covers the pure half of a tick end to end: feature building over a fully
populated window (26 features, 13 optional sensors present), all 11 detector rules, triage
(fingerprinting, gates, dedup), and deterministic summary rendering. The database round trip is
deliberately excluded so the number stays the pipeline's own cost and remains comparable as the
catalog grows; the persistence half is covered by `api-incidents-catalog` and by
`tests/integration/test_monitoring.py`.

## Reading these numbers

- **A tick is not a cost problem.** At 0.2 ms against a 60 s evaluation window, the notifier is
  ~5 orders of magnitude inside its budget. The alert budget, not compute, is the binding
  constraint — which is the point of the design (ADR 0020 §2).
- **The read path is in line with the other catalog reads.** 7.4 ms p50 sits alongside
  `api-episodes-catalog` (6.9 ms) and `api-slice-manifest` (7.0 ms) from EXP-0002, so the
  incidents queue adds no new read-path risk. The dominant cost is request handling and
  serialization, not the incident query.
- **These are not production claims.** They are one laptop, one dataset, n=10 for the read path.
  No baseline was written; the results are recorded here for review per the harness contract.

## Not measured, and why

- **Detection accuracy.** This requires the chaos harness (plan §6) with fault injection against
  the real system. That harness is designed and documented but not built, so there is no
  precision/recall figure here yet, and none is claimed.
- **Wall-clock behaviour.** A tick is cheap, but a tick that has never run is a silent monitor.
  Blindness and staleness are covered by `GET /api/v1/monitoring/health` and by integration tests,
  not by a latency number.
- **Catalog growth.** The snapshot is a single bounded query by design, but its cost against a
  catalog two orders of magnitude larger is untested.

## Next

- B-016: build the chaos harness and produce the first real precision-at-budget figure.
- B-017: re-measure `monitor-evaluate` against a catalog with a large episode population.
