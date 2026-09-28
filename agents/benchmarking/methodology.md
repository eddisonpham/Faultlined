# Benchmark Methodology (definitive)

Change this document only through an ADR.

## Rules
1. **Controlled workload.** Named, versioned workload definition (dataset version, size, concurrency, config). Synthetic and real-data workloads are both labeled.
2. **Warmup** runs are executed and discarded (default: 3, or until steady state; justify deviations).
3. **Repeated trials** (default: ≥ 10 for micro-benchmarks, ≥ 5 for end-to-end). Report all raw samples.
4. **Report** median plus P50/P95/P99 for latencies; mean ± stddev and 95% CI for throughput. Never a single number.
5. **Isolation.** Record background load; pin what can be pinned (CPU governor, GPU clocks if permitted); one variable changes at a time.
6. **Baselines.** A committed baseline exists per benchmark+hardware profile. Regression = statistically meaningful degradation beyond a documented threshold; the compare tool must state which test it uses.
7. **Provenance is mandatory.** A result without provenance is invalid.
8. **Storage.** Raw results are machine-readable (schema in `metric-schema.md`), stored outside git under `benchmarks/results/` (gitignored); baselines (small JSON) are committed under `benchmarks/baselines/`. Interpretations live in `agents/experiments/`.
9. **Every performance claim** in docs or on a resume must link to an experiment record.

## Required provenance fields
git commit (+dirty flag), config (full), dataset + version, model + version (if any), hardware (CPU/RAM/GPU/driver/disk),
software/environment (OS, language versions, lockfile hash, container image digest), random seed(s), timestamp (UTC),
workload name + version, relevant system parameters (concurrency, batch size, worker count, clocks/power mode).

## Metrics catalog (record where applicable; each must inform a decision)
Throughput; P50/P95/P99 latency; queue time; end-to-end latency; startup time; failure rate;
CPU/GPU utilization; RAM/VRAM; disk and network I/O; scaling behavior (workers, data size, concurrency); cost/resource efficiency
(e.g., episodes/GPU-hour, episodes/core-second).

## Layout
```text
benchmarks/
├── workloads/     # workload definitions (versioned)
├── baselines/     # committed small JSON baselines per hardware profile
└── results/       # gitignored raw runs
```
Harness location and CLI: TODO(phase 06).
