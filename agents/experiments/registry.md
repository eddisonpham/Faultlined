# Experiment Registry

No performance experiment has a reviewed result or committed baseline yet. A harness unit test is not an experiment record. See [benchmark backlog](../benchmarking/backlog.md) and [methodology](../benchmarking/methodology.md).

## Current production configuration
| Setting | Value | Why (EXP / ADR) |
|---|---|---|
| No production configuration | Project is scaffolding; no production deployment | — |

## Tested alternatives
| ID | What | Outcome | Link |
|---|---|---|---|
| — | None recorded | No measured experiment results available | — |

## Rejected approaches
| ID | What | Why rejected | Link |
|---|---|---|---|
| ADR 0008 | Add OpenTelemetry/Prometheus/Grafana at scaffolding scale | Deferred due to absent scraper/services and single-host scope; see ADR trigger for revisiting | [ADR 0008](../decisions/0008-observability.md) |
| ADR 0013 | Platform-specific Windows host telemetry | Rejected in favor of cross-platform psutil API; design decision only, not a measured experiment | [ADR 0013](../decisions/0013-cross-platform-resource-telemetry.md) |

## Future / deferred ideas
| Idea | Why deferred | Revisit when |
|---|---|---|
| Authorize, run, review, and commit a first synthetic-ingest baseline (EXP-0001) | Requires an explicitly authorized measurement and comparison rule/hardware checks; no baseline is committed | Owner authorizes collecting a measured baseline and harness correctness blockers are addressed |
| Real MCAP / LeRobot benchmark | Readers and fixed versioned fixtures do not exist | MVP ingest supports a public/small reproducible dataset subset |
| Queueing, end-to-end, failure recovery, scaling, startup, and efficiency metrics | Runtime metric primitives are not wired; lifecycle and production components are incomplete | Metric instrumentation and relevant implementation stages exist |
| OpenTelemetry tracing | Correlation IDs and one local API/worker topology are sufficient for this slice | At least three independently communicating services or a trace-driven incident; supersede ADR 0008 |
| Prometheus exposition / dashboard | No scraper and no operational UI exist | A real scrape/alert or dashboard use case exists |
