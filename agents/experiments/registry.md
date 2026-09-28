# Experiment Registry

EXP-0001 holds the first reviewed result and committed baseline. A harness unit test is still not an experiment record. See [benchmark backlog](../benchmarking/backlog.md) and [methodology](../benchmarking/methodology.md).

## Current production configuration
| Setting | Value | Why (EXP / ADR) |
|---|---|---|
| No production configuration | Project is scaffolding; no production deployment | — |

## Tested alternatives
| ID | What | Outcome | Link |
|---|---|---|---|
| EXP-0001 | Synthetic episode ingest microbenchmark baseline | P50 0.6084 ms over 10 trials, 0 failures, clean tree at `f7ffbeb`; verification pass −23.5%, no regression. Synthetic 199-byte payload only — not a throughput or production claim | [EXP-0001](0001-synthetic-ingest-baseline.md) |

## Rejected approaches
| ID | What | Why rejected | Link |
|---|---|---|---|
| ADR 0008 | Add OpenTelemetry/Prometheus/Grafana at scaffolding scale | Deferred due to absent scraper/services and single-host scope; see ADR trigger for revisiting | [ADR 0008](../decisions/0008-observability.md) |
| ADR 0013 | Platform-specific Windows host telemetry | Rejected in favor of cross-platform psutil API; design decision only, not a measured experiment | [ADR 0013](../decisions/0013-cross-platform-resource-telemetry.md) |

## Future / deferred ideas
| Idea | Why deferred | Revisit when |
|---|---|---|
| Real MCAP / LeRobot benchmark | Readers and fixed versioned fixtures do not exist | MVP ingest supports a public/small reproducible dataset subset |
| Queueing, end-to-end, failure recovery, scaling, startup, and efficiency metrics | Queue/failure metrics are emitted but nothing aggregates them into a benchmark result; lifecycle and production components are incomplete | B-004 aggregation is implemented and the job lifecycle exists |
| OpenTelemetry tracing | Correlation IDs and one local API/worker topology are sufficient for this slice | At least three independently communicating services or a trace-driven incident; supersede ADR 0008 |
| Prometheus exposition / dashboard | No scraper and no operational UI exist | A real scrape/alert or dashboard use case exists |
