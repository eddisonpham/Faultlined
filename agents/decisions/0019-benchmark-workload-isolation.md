# ADR 0019: Benchmark workloads run in isolated processes

- **Status:** Accepted (2026-09-29)
- **Deciders:** owner + benchmark-engineer
- **Supersedes/amends:** amends `agents/benchmarking/methodology.md` (Rule 5, Isolation)

## Context

EXP-0002 measured the run-intelligence workloads. Running all workloads sequentially inside one
`de bench --workload all` process inflated `synthetic-episode-ingest` from 0.539 ms to 12.86 ms p50 (~24×)
and tripped the regression alarm falsely. cProfile attributes the inflation to filesystem operations in the
artifact store's `_publish` (fsync, hardlink, temp-file churn): earlier workloads (e.g. `lerobot-ingest-v3`,
which writes real Parquet-sized blobs) leave the Windows filesystem cache and Defender real-time scanning
under pressure, and later micro workloads pay for it. Isolated per-workload processes were repeatable
(std dev ≤ 3.7 ms across all eight workloads).

## Decision

**Run each benchmark workload in its own process when measuring.** Batch in-process runs (`--workload all`)
are smoke-only and must not feed result records, baselines, or regression verdicts. The harness keeps
`--workload all` for CI-style smoke; measurement procedure is one process per workload.

## Consequences

- Measurement procedure is one extra step (a loop over workload names); numbers are comparable across runs.
- Regression comparisons and baselines remain per-workload; no change to baseline storage.
- The methodology's Rule 5 (Isolation) now covers cross-workload interference within a batch run.
- Revisit if the harness grows a `--isolate` flag that spawns subprocesses per workload (nice-to-have, not required).

## Alternatives considered

- **Drop `--workload all` entirely:** rejected — useful as a smoke test in CI and for catching harness breakage.
- **Serialize with cooldown sleeps between workloads:** rejected — sleeps don't address Defender/cache
  pressure and slow every run; process isolation is deterministic.
- **Reset filesystem state between workloads:** rejected — cannot control Defender from the harness.
