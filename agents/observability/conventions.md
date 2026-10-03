# Observability Conventions

Implemented: JSON logging, correlation IDs, host/GPU telemetry with per-field fallback, and runtime metric emission at worker/ingest call sites. Libraries: stdlib logging/JSON, psutil host sampling (ADR 0013), optional NVML GPU sampling (ADR 0008). Host psutil errors yield `null` fields rather than exceptions. Host-level `system_*` gauges are sampled on a runtime interval by the worker loop, not only by the benchmark harness.

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
- One-shot sampling is used at benchmark start/end and by the runtime sampler. CPU percent is interval-dependent (first sample may be 0); network fields are cumulative counters, not deltas.
- Runtime sampling: `RuntimeMetrics.sample_host()` takes one sample and `emit_host_sample()` writes the gauges; `de worker`/`de dev` call it every `HOST_SAMPLE_INTERVAL_SECONDS` (60 s) from the worker loop. A field the sampler could not read is **omitted**, not written as `0` — an absent gauge is honest, a fabricated zero is not. GPU gauges are emitted only when NVML answers; no GPU means no `system_gpu_*` record, not a zero. A sampler that raises is logged as `event: host_sample_failed` and skipped (ADR 0008: telemetry cannot stop the worker).

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
| `monitor_tick_seconds` | sample | seconds | none | Whether the monitor loop keeps its interval or is starving |
| `monitor_signals_total` | counter | count | `label`, `severity` | Which conditions the monitor actually observes |
| `monitor_suppressed_total` | counter | count | `action` | Whether dedupe/budget is hiding real incidents |
| `monitor_sensor_availability` | gauge | ratio (0–1) | none | Whether the monitor can see enough sensors to judge anything |
| `monitor_blind` | gauge | boolean | none | The monitor is not observing the platform; read with the health endpoint, never paged as an incident |
| `incidents_opened_total` | counter | count | `label`, `severity` | How often each condition escalates |
| `incidents_notified_total` | counter | count | `label` | Which incidents actually reached a human (emits 0 when the notify class says don't page) |

Labels are deliberately low-cardinality; IDs belong in logs/result provenance, never metric labels.

**Emission status.** `RuntimeMetrics` emits these signals at their call sites: `jobs_queue_depth`, `jobs_queue_time_seconds`, `jobs_run_time_seconds`, `jobs_failures_total`, `jobs_retries_total`, `jobs_cancellations_total`, `jobs_timeouts_total`, `pipeline_stage_duration_seconds`, `episodes_ingested_total`, `artifacts_written_bytes_total`, `api_request_duration_seconds`/`api_requests_total` (API middleware), `catalog_query_duration_seconds` (repository wrapper), and `workers_heartbeat_age_seconds` (worker, every claim attempt; value 0, the record timestamp is the heartbeat). They are appended as JSONL to `DE_METRICS_PATH` (default `var/metrics/runtime.jsonl`, gitignored) by `de worker`/`de dev`/the API process. Sink failures are logged and swallowed so telemetry never breaks the pipeline. `RuntimeMetrics.emit_host_sample()` emits the `system_*` host/GPU gauges from the worker loop's 60 s sample, and `emit_host_sample` is also what the benchmark harness uses for its start/end deltas. The monitor loop emits `monitor_tick_seconds`, `monitor_signals_total`, `monitor_suppressed_total`, `monitor_sensor_availability`, `monitor_blind`, and `incidents_opened_total`/`incidents_notified_total`. Every registry row above is emitted by some call site. Not yet emitted: per-format validation/episode signals from unimplemented stages.

**Read-back (ADR 0017).** `data_engine.observability.aggregate` derives summaries (nearest-rank p50/p95/p99 per metric+labels), bucketed mean series, and worker heartbeat age from the sink; `GET /api/v1/metrics` serves it (newest 200k records, optional `?window_seconds=`/`?bucket_seconds=`). The sink remains the source of truth — no metrics server, no scraper.

**Lifecycle counter semantics (ADR 0015).** `jobs_failures_total` counts *attempts* that failed, including ones that are about to be retried; the job's final resting state is not derivable from it alone — subtract nothing, read `jobs_retries_total` alongside it. `jobs_retries_total` is labelled with the attempt that just failed (1-based), not the attempt that will run next. `jobs_timeouts_total` is emitted only when the deadline check is what ended the job: a job that times out is not also counted in `jobs_failures_total`, because it never reached the handler.

## Queries

The sink is a JSONL file, so everything below is either an HTTP read of the aggregation
layer or a one-liner over the file. There is no metrics server, no scraper, and no
`jq` dependency (not installed on every dev box — the Python forms are canonical).

### HTTP

```bash
# Is the pipeline healthy right now? queue depth per state + live host resources.
curl -s localhost:8000/api/v1/status

# What happened in the last 15 minutes? per-metric+labels summaries
# (p50/p95/p99/min/max/mean/sum/count) and 60 s bucketed mean series.
curl -s 'localhost:8000/api/v1/metrics?window_seconds=900&bucket_seconds=60'

# p95 of every recorded metric in the sink tail (newest 200k records, no window).
curl -s localhost:8000/api/v1/metrics | python -c \
  "import json,sys; [print(f\"{s['name']:42} p95={s['p95']:<12} n={s['count']}\") \
   for s in json.load(sys.stdin)['summaries']]"

# Is a worker alive? None means no heartbeat has ever been recorded.
curl -s localhost:8000/api/v1/metrics | python -c \
  "import json,sys; print(json.load(sys.stdin)['worker_heartbeat_age_seconds'])"
```

`/ui/metrics` renders the same model as a dashboard; add `?partial=1` to any UI route for
the HTML fragment used by live polling.

### Over the sink file (`var/metrics/runtime.jsonl`)

```bash
# Failure reasons, most common first — which failures deserve reliability work.
grep -o '"name": "jobs_failures_total".*' var/metrics/runtime.jsonl \
  | grep -o '"reason_code": "[a-z_]*"' | sort | uniq -c | sort -rn

# Did retries turn into permanent failures? both counters, by attempt/reason.
python - <<'PY'
import json, collections
counts = collections.Counter()
for line in open("var/metrics/runtime.jsonl", encoding="utf-8"):
    try:
        r = json.loads(line)
    except ValueError:
        continue  # a truncated tail line after a crash is not a metric
    if r["name"] in ("jobs_failures_total", "jobs_retries_total", "jobs_timeouts_total"):
        counts[(r["name"], r.get("labels", {}).get("reason_code"))] += 1
for key, n in counts.most_common():
    print(n, key)
PY

# The monitor's own health: is it running, is it seeing anything, is it paging?
grep -E '"name": "(monitor_blind|monitor_sensor_availability|incidents_opened_total)"' \
  var/metrics/runtime.jsonl | tail -20
```

Logs go to stdout under `de run`; when you redirect them yourself (the forms above assume
`var/logs/*.log`) the same greps work.

```bash
# The same, from logs: what ended a job, with its correlation id.
grep '"event": "job_failed_permanently"' var/logs/*.log | tail -20
```

### Log events worth alerting on

`job_claimed` / `job_succeeded` / `job_failed` / `job_retry_scheduled` /
`job_failed_permanently` are the job lifecycle; `jobs_reaped` means a deadline was swept
or a dead worker's job was reclaimed. Platform health signals are
`metric_write_failed` (telemetry sink unwritable), `database_unavailable` (the API
answered 503 with `Retry-After: 5`), `host_sample_failed`, and the `monitor_*` family
(`monitor_sink_unreadable`, `monitor_probe_failed`, `monitor_resources_failed`,
`monitor_baseline_load_failed`, `monitor_baseline_save_failed`,
`monitor_contract_read_failed`, `monitor_persist_failed`, `monitor_triage_state_failed`).
Every one of them carries `correlation_id`, so
`grep '"correlation_id": "<id>"' var/logs/*.log` reconstructs one job's whole path.

## Tracing

Distributed tracing is **not warranted now**: one API process + one local worker process class, stable correlation IDs, and structured stage/job logs are enough. This follows ADR 0008; revisit when ≥3 independent services communicate concurrently or a trace-driven incident requires it (then supersede via ADR).
