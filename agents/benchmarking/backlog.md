# Benchmark Backlog

Planned metrics and workloads. No real benchmark baseline or experiment has been committed yet. Move a metric to an experiment record only after collecting a result with complete provenance and a reviewed baseline where applicable.

| ID | Benchmark / metric | Question it answers | Metrics | Status |
|---|---|---|---|---|
| B-001 | Synthetic episode ingest microbenchmark | What is the local overhead of canonicalizing one small synthetic JSON episode and writing it to the filesystem artifact store? | Per-trial latency P50/P95/P99, mean, standard deviation, bootstrap CI; failure count; host samples | Harness implemented (`synthetic-episode-ingest`); baseline not measured/committed; catalog is in-memory, so this is not DB/API/MCAP throughput |
| B-002 | MCAP streaming ingest throughput | Can one worker sustain the provisional 50 MB/s target on local NVMe for realistic robot episodes? | MB/s, CPU/RAM, disk read/write, failure rate | Not implemented; requires a versioned controlled MCAP fixture and real reader |
| B-003 | LeRobot directory ingest | What are parse and registration costs for a real LeRobot v3 episode dataset? | Episodes/s, latency percentiles, CPU/RAM/disk, failure rate | Not implemented; requires reader and fixed dataset subset |
| B-004 | API enqueue-to-worker-start queue time | How long does an accepted job wait before execution at varying queue depth? | Queue-time P50/P95/P99, enqueue throughput, failure rate | `jobs_queue_depth` and `jobs_queue_time_seconds` are emitted to the JSONL sink, but nothing aggregates them into a benchmark result and no baseline exists; retries/leases are not implemented |
| B-005 | End-to-end ingest latency | What is submit → persisted artifact/catalog completion time for a real supported input? | End-to-end P50/P95/P99, failure rate, CPU/RAM/disk | Not implemented as a controlled benchmark; current harness excludes API/PostgreSQL |
| B-006 | Worker scaling | Does throughput improve with worker count, and where does contention appear? | Episodes/s, queue latency, CPU/RAM, DB and disk I/O | Deferred until durable lifecycle, real ingest and runtime metrics exist |
| B-007 | Data-size scaling | How do latency and memory scale with episode/file size? | Throughput, latency percentiles, peak RSS, disk/network I/O | Deferred; harness currently uses one tiny synthetic payload |
| B-008 | Failure rate / recovery | What fraction of jobs fail and recover under malformed data, worker crash, DB/disk faults? | Failure rate, retry/recovery time, orphan count | `jobs_failures_total` is emitted with typed reason codes, but no benchmark drives it; retries, leases, fault injection, and full recovery are not implemented |
| B-009 | Startup time | What is cold/warm API and worker startup cost on the local host? | Startup latency, CPU/RAM | Not implemented |
| B-010 | Resource efficiency / optional GPU workload | What resources are consumed per episode or workload, and is optional GPU telemetry useful? | Episodes/core-second, episodes/GPU-hour when relevant, RAM/VRAM, utilization | Host telemetry snapshot exists; no GPU workload or efficiency benchmark |
| B-011 | Network I/O / transfer cost | What is the cost of remote dataset fetch/storage when that path is introduced? | Bytes/s, transfer latency, failures, cost per episode | Deferred; local-only path has no remote transfer |

## Current harness boundaries

`benchmarks/harness.py` runs three warmups and ten measured trials for a synthetic in-memory catalog plus real local filesystem artifact store. It preserves raw trial latencies and host resource snapshots. The checked-in baseline directory intentionally contains only `.gitkeep`; `just bench` currently needs a reviewed baseline file and otherwise fails after writing an ignored raw result. Do not treat the unit test invoking this function as a published performance result.
