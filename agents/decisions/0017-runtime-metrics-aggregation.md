# ADR 0017: Aggregate the JSONL metrics sink for read-back

- **Status:** accepted, 2026-09-29
- **Deciders:** owner + implementer
- **Supersedes:** none. Extends [ADR 0013](0013-cross-platform-resource-telemetry.md) and the
  [observability conventions](../observability/conventions.md).

## Context

The runtime emits registry metrics to a JSONL sink (`DE_METRICS_PATH`), but nothing reads them back:
operators had to `tail` a file to see queue depth trends or stage latencies, and the plan for this slice
demanded expandable monitoring plus interactive performance visualization. Meanwhile the observability
registry promised `api_request_*`, `catalog_query_*`, and `workers_heartbeat_age_seconds` signals that had
no emitter. The alternative — a metrics server (Prometheus client, OpenTSDB) — is a dependency and a
process, and the project is a local-first single-process-platform (`just run`) whose conventions already
name the JSONL sink the emission target.

## Decision

1. **The JSONL sink stays the source of truth.** A new pure module
   (`data_engine/observability/aggregate.py`) reads records and derives summaries (count/sum/mean/min/max,
   nearest-rank p50/p95/p99 per metric+labels), bucketed mean series for sparklines, and heartbeat age.
   No new storage, no new process.
2. **Request latency is recorded per route template** (`api_request_duration_seconds` +
   `api_requests_total`, labels `route`, `method`, `status_class`) by an HTTP middleware in the API app.
   Raw paths are never labels (cardinality rule); unmatched requests are labelled `unmatched`.
3. **Catalog operations time themselves.** Every public `PostgresCatalog` method is wrapped once at class
   definition (`_timed_operation`) and emits `catalog_query_duration_seconds` with the method name as the
   `operation` label. Future methods are instrumented automatically; underscore helpers are not.
4. **Worker heartbeats are records, not gauges.** The worker emits
   `workers_heartbeat_age_seconds` (value 0, labels `worker_state=busy|idle`) at every claim attempt; the
   *age* is derived at read time from the newest record's timestamp. A stale gauge would need a clock in
   the emitter; the record timestamp is already authoritative.
5. **`GET /api/v1/metrics`** exposes the aggregate: summaries, series, live job-state counts, and
   heartbeat age. It reads at most the newest 200k records and supports `?window_seconds=` and
   `?bucket_seconds=`.

## Consequences

- Dashboards (UI work package) and the API read the same sink the worker writes; no scraping, no daemon.
- Aggregation cost grows with record count; the `max_records` bound keeps the endpoint local-fast, and a
  benchmark (B-015) quantifies the summarizer so the bound can be tuned.
- Percentiles over a window are nearest-rank over retained samples; this is descriptive telemetry, not a
  billing-grade SLI. Exact-window percentiles over unbounded history would require a real TSDB — revisit
  if the sink routinely exceeds the record bound.
- If emission volume or retention becomes a problem (rotation, multiple hosts), supersede this ADR with a
  real metrics backend; the aggregate module's input is a record iterator, so the transition is contained.
