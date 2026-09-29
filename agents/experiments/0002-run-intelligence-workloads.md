# EXP-0002: Run-intelligence workload measurements (B-003, B-012..B-015)

- **Date (UTC):** measured 2026-09-29T14:40Z–15:10Z
- **Author/agent:** benchmark-engineer
- **Status:** done (first measurements of these workloads; no baselines written — owner authorization required per methodology)
- **Related ADR / requirement:** ADR 0017 (runtime metrics aggregation); ADR 0018 (episode quality signals); benchmark backlog B-003, B-012..B-015

## Hypothesis / purpose

Quantify the latency of every run-intelligence feature added in the run-intelligence slice: real LeRobot v3
ingest (B-003), motion-quality analysis at 303 and 3000 frames (B-012), validation evaluation (B-013),
metrics aggregation (B-014), and the API/UI read paths (`/api/v1/metrics`, `/ui/insights`, B-015). All numbers
are local single-host measurements with the methodology defaults (3 warmups, 10 trials) except
`lerobot-ingest-v3` (1 warmup, 5 trials, real fixture data) — see Configuration.

## Change under test

No optimization was tested. This run records first measurements for the new workloads and re-measures
`synthetic-episode-ingest` after the quality-analysis work (package B) landed in its ingest path.

## Configuration

Workloads from `benchmarks/harness.py` v1.1.0, run via `uv run --all-extras python -m benchmarks.harness
--workload <name>` — **one workload per process** (see Trade-offs for why). Micro workloads: 3 warmups,
10 trials, 1 worker. `lerobot-ingest-v3`: 1 warmup, 5 trials over episode `episode_index=7` (203 frames) of
`lerobot/svla_so101_pickplace` tabular slice (v3.0). Catalog adapters are in-memory; artifact bytes go to the
production filesystem artifact store (`var/benchmark-artifacts`). Not a Postgres benchmark.

## Provenance

| Field | Value |
|---|---|
| git commit | `419a4ca3b61adf7ea8de0ce1d85a72cfbe636d70` (+ 2 uncommitted files: the harness fixture-path fix and `tests/unit/test_benchmark_workloads.py`) |
| git dirty | `true` (harness path fix `var/real-data/svla_so101_pickplace`; results unaffected — same fixture bytes) |
| lockfile hash | `67078559e9ced8dea2b4782059ddbe15690905a0e503130a3166dde830c49c3a` |
| OS / Python / app | Windows 11 10.0.26200 SP0 / 3.14.5 / 0.1.0 |
| seed | 20260928 |
| hardware | Intel64 Family 6 Model 198 Stepping 2, 24 logical CPUs, 33.75 GB RAM, disk `C:\` |
| GPU | NVIDIA GPU (NVML), 8.55 GB (unused by these workloads) |
| background load | No controlled background load; owner machine interactive |
| result files | `benchmarks/results/*.json` (gitignored; one `run_id` per workload) |

## Results

Isolated runs (one workload per process; the numbers to cite):

| Workload | n | P50 (ms) | P95 (ms) | Mean (ms) | Std dev (ms) | Failures |
|---|---|---|---|---|---|---|
| synthetic-episode-ingest | 10 | 0.539 | 0.749 | 0.581 | 0.098 | 0/10 |
| lerobot-ingest-v3 (203f, real) | 5 | 17.631 | 19.003 | 17.390 | 1.293 | 0/5 |
| quality-analysis-303f | 10 | 2.584 | 3.518 | 2.736 | 0.452 | 0/10 |
| quality-analysis-3000f | 10 | 23.090 | 28.170 | 23.721 | 2.065 | 0/10 |
| validation-eval | 10 | 0.151 | 0.245 | 0.152 | 0.051 | 0/10 |
| metrics-aggregation | 10 | 43.822 | 54.658 | 45.389 | 3.728 | 0/10 |
| api-metrics-endpoint | 10 | 47.641 | 74.291 | 52.038 | 9.065 | 0/10 |
| ui-insights-page | 10 | 6.910 | 8.484 | 6.942 | 0.751 | 0/10 |

`synthetic-episode-ingest` vs committed baseline (EXP-0001): P50 0.539 ms vs 0.6084 ms, **−0.1%,
`regression: false`**. The quality analysis added to the ingest path costs ~0.2 ms per synthetic episode
(cProfile: `analyze` 0.2 ms of 2.2 ms total; artifact `_publish` dominates at ~20 ms of 22 ms profiled
time). No regression, no baseline rewrite needed.

Derived observations (first measurements, not claims):

- Quality analysis scales ~linearly with frames in this range: 2.58 ms @ 303f → 23.09 ms @ 3000f
  (~7.7 µs/frame/dim set), comfortably below the real-time frame budget of a 30 fps robot stream.
- Real LeRobot v3 ingest (read + slice + quality + content-addressed artifact write) is ~18 ms/episode for a
  203-frame episode — the artifact store write dominates.
- `metrics-aggregation` and `api-metrics-endpoint` at 44–48 ms p50 are dominated by reading the JSONL metric
  sink tail; acceptable for an on-demand operator endpoint at scaffolding scale, revisit if the sink grows
  unbounded (B-004 already flags periodic compaction).

## Trade-offs

**Single-process interference finding (important methodology result):** running all workloads sequentially in
one `--workload all` process inflated `synthetic-episode-ingest` from 0.54 ms to 12.86 ms p50 (~24×) and
tripped the regression alarm falsely. cProfile in isolation attributes the extra time to filesystem operations
(`nt.fsync`, `nt.link`, `nt.mkdir`, `nt.unlink` in artifact `_publish`) — the earlier LeRobot workload's
artifact writes leave the Windows filesystem cache / Defender scanning under pressure. Isolated processes give
stable, repeatable numbers (std dev ≤ 3.7 ms on all workloads). Procedure adopted: measure each workload in
its own process; treat in-process batch runs as smoke-only. Recorded in
`agents/benchmarking/methodology.md` (Measurement hygiene) and the backlog.

## Conclusion

Decision: **adopt these as first recorded measurements** (not baselines). Baselines are left unwritten pending
owner authorization, per the backlog rule. `synthetic-episode-ingest` stays on its existing baseline with no
regression.

## Addendum (2026-09-29, package E run-inspection endpoints)

Two more B-014 workloads measured with the same procedure (isolated processes, 3 warmups,
10 trials, in-process TestClient + stub catalog): `api-episodes-catalog` (50-row page with
quality columns) P50 6.894 ms / P95 8.472 ms; `api-episode-validation` (one profile verdict
with violations) P50 5.718 ms / P95 7.027 ms. Both 0/10 failures. Raw results under
`benchmarks/results/` with the same provenance shape as the table above.

## Addendum (2026-09-29, run-report endpoint)

Same procedure (isolated process, 3 warmups, 10 trials, in-process TestClient + stub
catalog): `api-job-report` (run triage card over 50 produced episodes) P50 6.692 ms /
P95 9.492 ms, 0/10 failures.

## Follow-ups

- Owner authorization to promote any of these numbers to committed baselines (`--write-baseline` per workload).
- B-004: aggregate the already-emitted queue/failure metrics into end-to-end job-lifecycle workloads.
- If the JSONL sink becomes a bottleneck, add bounded tail reads or compaction (trigger: p50 > 100 ms).
