# Experiment Registry

EXP-0001 holds the first reviewed result and committed baseline. EXP-0002 records the first run-intelligence workload measurements. EXP-0003 records the deterministic notifier's first latency measurements. Neither EXP-0002 nor EXP-0003 wrote a baseline. A harness unit test is still not an experiment record. See [benchmark backlog](../benchmarking/backlog.md) and [methodology](../benchmarking/methodology.md).

## Current production configuration
| Setting | Value | Why (EXP / ADR) |
|---|---|---|
| No production configuration | Project is scaffolding; no production deployment | — |

## Tested alternatives
| ID | What | Outcome | Link |
|---|---|---|---|
| EXP-0001 | Synthetic episode ingest microbenchmark baseline | P50 0.6084 ms over 10 trials, 0 failures, clean tree at `f7ffbeb`; verification pass −23.5%, no regression. Synthetic 199-byte payload only — not a throughput or production claim | [EXP-0001](0001-synthetic-ingest-baseline.md) |
| EXP-0002 | Run-intelligence workloads (B-003, B-012..B-015) | Isolated P50s: synthetic-ingest 0.539 ms (no regression vs EXP-0001), lerobot-ingest-v3 17.6 ms (203f real), quality 2.58 ms@303f / 23.1 ms@3000f, validation-eval 0.151 ms, metrics-aggregation 43.8 ms, api-metrics 47.6 ms, ui-insights 6.9 ms. Key finding: batch in-process runs inflate micro benchmarks ~24× (ADR 0019) | [EXP-0002](0002-run-intelligence-workloads.md) |
| EXP-0003 | Deterministic notifier latency (B-016, B-017) | `monitor-evaluate` P50 0.2 ms / P95 0.3 ms over 50 trials with every baseline scope warm; `api-incidents-catalog` P50 7.4 ms / P95 9.8 ms (n=10), in line with the other catalog reads. Conclusion: compute is not the constraint, the alert budget is. No detection-accuracy figure yet — that needs the chaos harness | [EXP-0003](0003-deterministic-notifier-latency.md) |

## Rejected approaches
| ID | What | Why rejected | Link |
|---|---|---|---|
| ADR 0008 | Add OpenTelemetry/Prometheus/Grafana at scaffolding scale | Deferred due to absent scraper/services and single-host scope; see ADR trigger for revisiting | [ADR 0008](../decisions/0008-observability.md) |
| ADR 0013 | Platform-specific Windows host telemetry | Rejected in favor of cross-platform psutil API; design decision only, not a measured experiment | [ADR 0013](../decisions/0013-cross-platform-resource-telemetry.md) |
| ADR 0020 | A trained Isolation Forest over the feature vector, and an LLM in the detection path | No real labels to learn from, effective sample size far below what it needs, and a non-reproducible output for a system whose value is being trusted. Every MVP failure class has a known mechanism, so rules and control limits carry the whole design at 0.2 ms/tick with no dependency | [ADR 0020](../decisions/0020-deterministic-monitoring-notifier.md) |

## Future / deferred ideas
| Idea | Why deferred | Revisit when |
|---|---|---|
| Real MCAP / LeRobot benchmark | Readers and fixed versioned fixtures do not exist | MVP ingest supports a public/small reproducible dataset subset |
| Queueing, end-to-end, failure recovery, scaling, startup, and efficiency metrics | Queue/failure metrics are emitted but nothing aggregates them into a benchmark result; lifecycle and production components are incomplete | B-004 aggregation is implemented and the job lifecycle exists |
| OpenTelemetry tracing | Correlation IDs and one local API/worker topology are sufficient for this slice | At least three independently communicating services or a trace-driven incident; supersede ADR 0008 |
| Prometheus exposition / dashboard | No scraper and no operational UI exist | A real scrape/alert or dashboard use case exists |
| Learned multivariate anomaly detection for monitoring | Superseded as the MVP by deterministic rules and control limits; would need ≥ 30 days of real telemetry and ≥ 200 labelled incidents from the unlabelled-events channel, and must beat rules on Tier-3 faults without exceeding the FP budget | Those labels exist, and a held-out comparison exists (ADR 0020, plan §11 Phase 3) |
