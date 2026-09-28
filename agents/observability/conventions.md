# Observability Conventions

Implemented foundations (phase 06), not complete runtime instrumentation. Libraries: stdlib logging/JSON, psutil host sampling (ADR 0013), optional NVML GPU sampling (ADR 0008). Initial host psutil error fallback uses nullable fields; it is under verification. Runtime metrics below are a target registry, not all emitted today.

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
| `workers_heartbeat_age_seconds` | gauge | seconds | `worker_state` | Detect dead/stuck workers (once heartbeat leases are implemented) |
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

Labels are deliberately low-cardinality; IDs belong in logs/result provenance, never metric labels. The metric point and JSONL sink primitives exist, but runtime call sites for queue/job/stage metrics are not yet wired. The registry is a target list, not evidence those metrics are currently collected.

## Tracing

Distributed tracing is **not warranted now**: one API process + one local worker process class, stable correlation IDs, and structured stage/job logs are enough. This follows ADR 0008; revisit when ≥3 independent services communicate concurrently or a trace-driven incident requires it (then supersede via ADR).
