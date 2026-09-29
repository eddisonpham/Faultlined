# Observability Conventions

Implemented: JSON logging, correlation IDs, host/GPU telemetry with per-field fallback, and runtime metric emission at worker/ingest call sites. Libraries: stdlib logging/JSON, psutil host sampling (ADR 0013), optional NVML GPU sampling (ADR 0008). Host psutil errors yield `null` fields rather than exceptions. Host-level `system_*` gauges are sampled by the benchmark harness but not yet on a runtime interval.

## Principles
- Every metric or log field must inform a decision. Delete ones that don't.
- Structured (JSON) logs to stdout. Human-readable rendering is a dev-only formatter.
- A **correlation ID** flows API → queue → worker → storage → logs → metrics → UI. Job ID and episode ID are first-class fields.
- Never log secrets, tokens, or full file contents.

## Required log fields
`timestamp` (UTC, ISO 8601), `level`, `service`, `event` (stable snake_case name), `message`, `correlation_id`,
`job_id` (when applicable), `episode_id` (when applicable), `error.type` / `error.message` / `error.stack` (on errors).

## Metrics
- Naming: `<domain>_<object>_<unit>` snake_case with base units (`_seconds`, `_bytes`, `_total`).
- Labels are low-cardinality only (never job ID or episode ID as a label).
- Levels: system (host/GPU), platform (queue depth, job states, worker health), pipeline (stage durations), episode (validation results, sizes), error/failure.

## Resource telemetry
- Collector: `data_engine.observability.telemetry.sample_resources()` returns UTC timestamp, CPU percent, process RSS, system memory used/available, disk used/free, cumulative network bytes sent/received, and GPU fields if NVML is usable. Each host psutil field now degrades to `null` on psutil/OS errors; missing GPU still produces `gpu_present=false` and nullable GPU fields. A missing interface counter is represented as `null`.
- psutil provides host/process metrics on Windows/Linux. GPU via NVML when available; absent driver/device is `gpu_present=false`, with nullable GPU fields (no GPU ≠ error).
- One-shot sampling is used at benchmark start/end and may be called periodically by a runtime sampler. CPU percent is interval-dependent (first sample may be 0); network fields are cumulative counters, not deltas.

## Metric registry

| Name | Type | Unit | Labels | Decision it informs |
|---|---|---|---|---|
| `jobs_queue_depth` | gauge | jobs | `state` | Whether worker slots keep up with submitted work |
| `jobs_queue_time_seconds` | histogram/sample | seconds | `job_type` | Tune concurrency and user wait time |
| `jobs_run_time_seconds` | histogram/sample | seconds | `job_type`, `state` | Identify slow stages / failure cost |
| `jobs_failures_total` | counter | count | `job_type`, `reason_code` | Which failures merit reliability work |
| `jobs_retries_total` | counter | count | `job_type`, `attempt` | Whether a flaky source is being retried into the same failure (ADR 0015) |
| `jobs_cancellations_total` | counter | count | `job_type` | How often operators cancel work, and which types |
| `jobs_timeouts_total` | counter | count | `job_type` | Whether job deadlines are sized correctly (ADR 0015) |
| `workers_heartbeat_age_seconds` | heartbeat record | seconds | `worker_state` | Detect dead/stuck workers; age derived at read time from the newest record's timestamp (ADR 0017) |
| `api_request_duration_seconds` | histogram/sample | seconds | `route`, `method`, `status_class` | Which endpoints are slow; route is the template, never a raw path |
| `api_requests_total` | counter | count | `route`, `method`, `status_class` | Request rate and error-share per route |
| `catalog_query_duration_seconds` | histogram/sample | seconds | `operation` | Which catalog operations deserve indexing or batching |
| `system_cpu_percent` | gauge | percent | none | Whether pipeline is CPU-bound |
| `system_memory_used_bytes` | gauge | bytes | none | Stay within laptop memory budget |
| `process_rss_bytes` | gauge | bytes | `process_role` | Attribute memory usage to API/worker |
| `system_disk_free_bytes` | gauge | bytes | `volume` | Prevent disk-full artifact failures |
| `system_network_bytes_sent_total` | counter | bytes | `interface` | Quantify transfer cost when remote storage arrives |
| `system_network_bytes_recv_total` | counter | bytes | `interface` | Same |
| `system_gpu_utilization_percent` | gauge | percent | `device_index` | Whether optional GPU workloads keep device busy |
| `system_gpu_memory_used_bytes` | gauge | bytes | `device_index` | Avoid 8 GB VRAM OOM |
| `pipeline_stage_duration_seconds` | histogram/sample | seconds | `stage`, `status` | Locate ingest/validate/index/build bottlenecks |
| `episodes_ingested_total` | counter | episodes | `format`, `status` | Ingest throughput/failure trend |
| `artifacts_written_bytes_total` | counter | bytes | `kind` | Disk/storage growth |

Labels are deliberately low-cardinality; IDs belong in logs/result provenance, never metric labels.

**Emission status.** `RuntimeMetrics` emits these signals at their call sites: `jobs_queue_depth`, `jobs_queue_time_seconds`, `jobs_run_time_seconds`, `jobs_failures_total`, `jobs_retries_total`, `jobs_cancellations_total`, `jobs_timeouts_total`, `pipeline_stage_duration_seconds`, `episodes_ingested_total`, `artifacts_written_bytes_total`, `api_request_duration_seconds`/`api_requests_total` (API middleware), `catalog_query_duration_seconds` (repository wrapper), and `workers_heartbeat_age_seconds` (worker, every claim attempt; value 0, the record timestamp is the heartbeat). They are appended as JSONL to `DE_METRICS_PATH` (default `var/metrics/runtime.jsonl`, gitignored) by `de worker`/`de dev`/the API process. Sink failures are logged and swallowed so telemetry never breaks the pipeline. Not yet emitted: the `system_*` host/GPU gauges and per-format validation/episode signals from unimplemented stages.

**Read-back (ADR 0017).** `data_engine.observability.aggregate` derives summaries (nearest-rank p50/p95/p99 per metric+labels), bucketed mean series, and worker heartbeat age from the sink; `GET /api/v1/metrics` serves it (newest 200k records, optional `?window_seconds=`/`?bucket_seconds=`). The sink remains the source of truth — no metrics server, no scraper.

**Lifecycle counter semantics (ADR 0015).** `jobs_failures_total` counts *attempts* that failed, including ones that are about to be retried; the job's final resting state is not derivable from it alone — subtract nothing, read `jobs_retries_total` alongside it. `jobs_retries_total` is labelled with the attempt that just failed (1-based), not the attempt that will run next. `jobs_timeouts_total` is emitted only when the deadline check is what ended the job: a job that times out is not also counted in `jobs_failures_total`, because it never reached the handler.

## Tracing

Distributed tracing is **not warranted now**: one API process + one local worker process class, stable correlation IDs, and structured stage/job logs are enough. This follows ADR 0008; revisit when ≥3 independent services communicate concurrently or a trace-driven incident requires it (then supersede via ADR).
