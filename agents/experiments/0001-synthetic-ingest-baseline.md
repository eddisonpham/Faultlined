# EXP-0001: Synthetic episode ingest microbenchmark baseline

- **Date (UTC):** planned 2026-09-28; no measurement performed
- **Author/agent:** benchmark-engineer
- **Status:** planned
- **Related ADR / requirement:** ADR 0012 (host-based development); FR-017; benchmark methodology

## Hypothesis / purpose

Establish a repeatable local reference for canonicalizing a small synthetic episode and writing it to the filesystem content-addressed artifact store. This is a harness smoke benchmark only; it does not establish real ingest throughput or an application performance claim.

## Change under test

No performance optimization is being tested. The planned baseline will measure the current `synthetic-episode-ingest` workload once the remaining baseline/comparison correctness findings in [the scaffolding review](../reviews/2026-09-28-scaffolding.md) are resolved and the owner explicitly authorizes a measured baseline.

## Configuration

Planned harness defaults: workload `synthetic-episode-ingest` version `1.0.0`; 3 warmup calls; 10 measured trials; one worker; one small synthetic JSON episode. The catalog adapter is in-memory; artifact bytes are written to the local filesystem. Full per-run configuration will be embedded in the result. This is not a Postgres/API benchmark and has no real MCAP or LeRobot dataset.

## Provenance

Not collected. A future result must include the actual commit + dirty flag, config hash, dataset fixture/version, hardware, OS/Python/lockfile/app versions, seed, UTC timestamp, workload version, system parameters, and background-load note. Raw output will be ignored under `benchmarks/results/`; a reviewed baseline belongs under `benchmarks/baselines/`.

## Results

No result file and no baseline exist. Do not infer timing from unit-test execution.

## Trade-offs

A synthetic local workload avoids network downloads and robotics dependencies but only exercises canonical JSON processing plus filesystem artifact writes. Its number cannot support MCAP ingestion, database, API, throughput, or production claims.

## Conclusion

Decision: **defer**. Collect and publish only after (1) owner authorization to write a measured baseline, (2) baseline input validation and remaining result-consistency checks are corrected, and (3) hardware comparison compatibility remains verified. The default `just bench` currently has no committed baseline to compare against.

## Follow-ups

- Resolve review findings 1–3 in `agents/reviews/2026-09-28-scaffolding.md`.
- Ask the owner before running `just bench --write-baseline` for a committed baseline.
- Review result provenance and noisy-machine caveats; then update this record and `agents/experiments/registry.md` with actual evidence.
