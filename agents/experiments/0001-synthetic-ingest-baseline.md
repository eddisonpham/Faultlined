# EXP-0001: Synthetic episode ingest microbenchmark baseline

- **Date (UTC):** measured 2026-09-28T18:28:08Z
- **Author/agent:** benchmark-engineer
- **Status:** accepted (first baseline; owner authorized 2026-09-28)
- **Related ADR / requirement:** ADR 0012 (host-based development); FR-017; benchmark methodology

## Hypothesis / purpose

Establish a repeatable local reference for canonicalizing a small synthetic episode and writing it to the
filesystem content-addressed artifact store. This is a harness smoke benchmark only; it does not establish real
ingest throughput or an application performance claim.

## Change under test

No performance optimization was tested. This run records the current `synthetic-episode-ingest` workload as the
reference point against which later changes are compared.

## Configuration

Workload `synthetic-episode-ingest` v1.0.0; 3 warmups; 10 measured trials; 1 worker; 199-byte synthetic JSON
episode. Catalog adapter is in-memory; artifact bytes written to the local filesystem. This is not a
Postgres/API benchmark and uses no real MCAP or LeRobot dataset.

## Provenance

| Field | Value |
|---|---|
| git commit | `f7ffbebacb543660b41af143a09f22f843d021fa` |
| git dirty | `false` |
| config hash | `72de0d0a22699b074f19f40e689ea2cee43ce8e60624d2d5bcf1e32845de16ad` |
| lockfile hash | `67078559e9ced8dea2b4782059ddbe15690905a0e503130a3166dde830c49c3a` |
| dataset / version | `synthetic-episode-v1` |
| OS / Python / app | Windows 11 10.0.26200 SP0 / 3.14.5 / 0.1.0 |
| seed | 20260928 |
| hardware | Intel64 Family 6 Model 198 Stepping 2, 24 logical CPUs, 33.75 GB RAM, disk `C:\` |
| GPU | NVIDIA GPU (NVML), 8.55 GB, driver available (present but unused by this workload) |
| background load | No controlled background load; owner machine interactive, GPU may be active |

Committed baseline: `benchmarks/baselines/synthetic-ingest-windows.json` (schema v1, `source_run_id`
`4e98cf6f-6e2a-4325-ae6c-32366141653e`). The raw schema-v2 result is intentionally gitignored under
`benchmarks/results/`, so the `source_run_id` resolves only on the measuring machine — see Follow-ups.

## Results

| Metric | Value (ms) |
|---|---|
| P50 | 0.6084 |
| P95 | 0.6721 |
| P99 | 0.6721 |
| Mean | 0.5741 |
| Std dev | 0.1272 |
| 95% CI of mean | 0.4862 – 0.6315 |
| Failures | 0 / 10 |

Verification pass (`just bench`, same commit, minutes later): P50 0.4652 ms, delta −0.1432 ms (−23.5%),
`regression: false`. The run-to-run swing of ~24% at this scale is larger than any plausible optimization effect
and is the main caveat on this number.

## Trade-offs

A synthetic local workload avoids network downloads and robotics dependencies but only exercises canonical JSON
processing plus filesystem artifact writes. At a 199-byte payload the measurement is dominated by fixed
per-call overhead, so it says nothing about scaling to real episode sizes. It cannot support MCAP ingestion,
database, API, throughput, or production claims. The hardware-profile gate means this baseline only compares on
this exact host; a different CPU, RAM, disk, GPU, or OS will refuse to compare rather than silently mix results.

## Conclusion

Decision: **accept as the first baseline.** It is reproducible, fully provenanced, and correctly refuses
comparison on mismatched hardware. It is a regression tripwire for local ingest-path overhead, not a performance
target.

## Follow-ups

- The `source_run_id` in the committed baseline points at a gitignored raw result. Commit the baseline's own raw
  result (or inline the trial latencies) so third parties can verify the percentiles without re-measuring.
- Ten trials with a ~24% observed spread is a weak sample. Consider raising the default trial count or pinning
  CPU affinity before using this benchmark to accept a real optimization.
- Next backlog items that extend this into something meaningful: B-002/B-003 (MCAP and LeRobot readers with
  versioned fixtures) and B-004 (aggregate the already-emitted queue-time metrics into a real result).
