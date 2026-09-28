# Phase 06 — Benchmark and observability foundations

Read `CLAUDE.md`, `agents/benchmarking/methodology.md` (binding), `agents/observability/conventions.md`, `agents/experiments/*`,
`agents/implementation/vertical-slice.md`. Use `benchmark-engineer` and `observability-engineer`.

## Observability
1. Structured JSON logging with the required fields; `correlation_id` propagated API → queue → worker → storage; job/episode IDs.
2. Metrics per naming conventions (base units, low-cardinality labels). Start with only signals that inform a decision:
   queue depth/time, job state counts and durations, stage durations, failures by type, worker health.
3. Resource telemetry collector: CPU/RAM/disk/network always; GPU via NVML when present; **must degrade gracefully without a GPU**.
4. Decide (ADR) whether distributed tracing is warranted now; default to correlation IDs + structured logs unless evidence says otherwise.
5. Fill the metric registry table in `observability/conventions.md`. Tests: log schema, correlation propagation, telemetry fallback.

## Benchmarking
1. Machine-readable **metric/result schema** with the provenance fields from `methodology.md` (git commit + dirty flag, config, dataset/model versions,
   hardware, software environment, seeds, UTC timestamp, workload version). Document in `benchmarking/metric-schema.md`.
2. Harness + CLI: named workloads, warmup, repeated trials, percentile/CI summaries, JSON results to `benchmarks/results/`,
   baseline save/compare with a documented regression rule.
3. One **real** micro-benchmark on the slice (e.g. enqueue→start latency, job throughput, or ingestion throughput) with a controlled
   workload; commit its baseline in `benchmarks/baselines/`.
4. Populate `benchmarking/backlog.md` with placeholders for every metric in the methodology catalog that is not yet implemented.
5. Write the first experiment record (`experiments/0001-…`) using the template and update the registry.
6. Tests: schema validation, percentile math, baseline compare (including a synthetic regression that must be flagged).

## Acceptance
- `bench` runs from a clean clone, produces a valid result file, and compares against the committed baseline.
- A result missing provenance is rejected by the harness.
- All gates green.

## Finish
Commit in logical pieces. Summarize the first benchmark's numbers with their caveats (do not overclaim; this is scaffolding).
