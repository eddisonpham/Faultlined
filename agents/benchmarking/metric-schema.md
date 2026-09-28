# Benchmark Metric / Result Schema

**Status: initial schema version 1 implemented; model/JSON Schema parity is not yet verified.** Python model:
`benchmarks/schema.py`; JSON Schema: [`result.schema.json`](result.schema.json). Schema changes increment `schema_version`; old results remain readable.

## Result envelope (`BenchmarkResult`)

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `schema_version` | integer | yes | Current value: `1` |
| `run_id` | UUID string | yes | Unique benchmark invocation |
| `benchmark` | object `{name, version}` | yes | Stable named workload |
| `status` | `ok` / `failed` | yes | Failure includes `failure` details |
| `started_at` | ISO-8601 UTC | yes | Invocation start |
| `duration_seconds` | number ≥ 0 | yes | Whole invocation duration |
| `config` | object | yes | Full benchmark config (warmup/trials/payload/workers) |
| `provenance` | object | yes | All mandatory provenance fields below; missing/empty required fields reject validation |
| `trials` | array of `Trial` | yes | Per-trial raw latency seconds, success flag, and resource sample references |
| `summary` | object | yes | n, warmup count, p50/p95/p99, mean, sample stddev, bootstrap CI95, failures |
| `resource_samples` | array of telemetry snapshots | yes | Time series with CPU/RAM/disk/network/GPU-if-present fields |
| `failure` | object or null | yes | Stable error type/message (no secrets) |

## Provenance (required)

`git_commit`, `git_dirty`, `config_hash`, `dataset` + `dataset_version` (synthetic fixtures use `synthetic-episode-v1`),
`model` + `model_version` (null for infrastructure microbench), `hardware` (CPU, logical CPUs, RAM bytes, GPU model/VRAM/driver
or explicit absent, disk), `software` (OS, Python version, package-lock hash, app version), `seeds`, `timestamp_utc`,
`workload` + `workload_version`, `system_parameters` (workers, payload bytes, warmups, trials), `background_load_note`.
Container image digest is `null` when not containerized (ADR 0012).

## Trial and statistics rules

- Raw samples are preserved; latency values in seconds, bytes in bytes, utilization in percent.
- Percentiles use nearest-rank on sorted samples: `ceil(p*n)-1` (clamped at 0); mean and sample standard deviation.
- 95% CI for the mean uses deterministic bootstrap resampling (seeded) with 2,000 resamples and percentile bounds.
- A result without complete provenance or at least one measured trial is invalid.
- The current comparator checks benchmark name/version and exact equality of the recorded CPU, logical CPU count, RAM, GPU descriptor, and OS hardware profile. It flags a latency median increase > 20% **and** an absolute increase > 1 ms. This is a coarse scaffolding alarm, not a statistical significance claim; raw repeated samples are always retained. Published JSON Schema / Pydantic parity and independent comparison validation remain open review follow-ups before using this as an accepted regression gate.

## Result storage

Raw invocation output: `benchmarks/results/<run_id>.json` (gitignored). Committed baselines:
`benchmarks/baselines/<benchmark>-<hardware-profile>.json`.
