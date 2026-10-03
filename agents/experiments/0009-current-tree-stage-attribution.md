# EXP-0009: Current-tree stage attribution of MCAP ingest — what profiling says is left

- **Date (UTC):** measured 2026-10-02
- **Author/agent:** benchmark-engineer
- **Status:** done
- **Related ADR / requirement:** NFR-001 (per-stage targets), stage-4 criterion "Bottlenecks identified by
  profiling (`benchmarks/` + telemetry), each addressed via an experiment record";
  [EXP-0004](0004-mcap-ingest-baseline.md) found the bottleneck, [EXP-0005](0005-mcap-ingest-flatten-plan.md)
  addressed it — this record is the stage-4 re-measurement of the same method against the current tree,
  plus the first cProfile hotspot pass over the other benchmarked engine stages.
- **Tool:** `scripts/profile_ingest.py` (new; the EXP-0005 attribution method made a command), raw dumps in
  gitignored `var/profile/`.

## Hypothesis / purpose

EXP-0005 left the engine's own stage at 0.465 s (21.5 MiB/s equivalent) and attributed the rest of the
gap to container iteration + JSON decode — stages nobody in this repository owns. Two questions are open
for stage 4 and this record answers both:

1. **On the current tree, is there any single addressable hotspot left above the format floor?** If
   profiling still showed one function dominating ingest the way `_dimensions` did (51% of total), it
   would be the next lever.
2. **Do the other engine stages (quality, validation, monitor, aggregation, API reads) hide a similar
   outlier**, or are their published numbers (EXP-0002, EXP-0003, EXP-0007) still the whole story?

## Method / change under test

No engine change. One profiling driver: `scripts/profile_ingest.py` measures, median of N passes after a
warmup, four stages of the same fixture (`so101_pick_place.mcap`, 20.1 MiB, SHA-256
`8973d57b…` as in EXP-0004/0005), then cProfiles one full ingest and one trial each of the harness
workloads. The stages are the same seams EXP-0005 used: container iteration only; + `json.loads`; full
reader (decode + flatten plan + statistics + quality window); full ingest (read + content-address + artifact
write) with an in-memory catalog and a throwaway artifact tree — the `mcap-ingest` workload's protocol.

## Configuration / provenance

| Field | Value |
|---|---|
| git commit / dirty | `6a20d7d` / dirty (55 files: cluster detail view, ADR 0028, this campaign's tooling) |
| OS / Python / app | Windows 11 10.0.26200 SP0 / 3.14.5 / 0.1.0 |
| hardware | Intel Family 6 Model 198, 24 logical CPUs, 33.75 GB RAM, disk `C:\` |
| background load | None controlled; **owner machine interactive** — see variance note |
| reps | 3 warmup-equivalents + measured passes; three separate runs recorded |

## Results

### Stage attribution (three runs, median of passes per stage)

| Stage | Run 1 | Run 2 | Run 3 (reps=5) |
|---|---|---|---|
| container iteration only | 0.200 s / 100.8 MiB/s | 0.258 s / 78.1 | 0.233 s / 86.3 |
| + `json.loads` (marginal) | +0.139 s / 0.339 s total | +0.247 / 0.505 | +0.210 / 0.444 |
| reader (engine stage, marginal) | +0.416 s / 0.755 total | +0.385 / 0.890 | +0.712 / 1.155 |
| full ingest (artifact marginal) | +0.120 s / 0.875 total | +0.031 / 0.921 | −0.166 / 0.990 |

The negative marginal in run 3 (full ingest *faster* than the reader stage measured moments earlier) is
the honest headline about precision: on this interactive machine, per-run spread is ±0.2–0.4 s, which is
the same order as the engine stage itself. What survives across runs is the *ordering* and the ratio
band, not any single number:

- **Container iteration: 0.15–0.31 s (78–131 MiB/s equivalent).** Not our code.
- **JSON decode marginal: 0.14–0.25 s.** C code. Not our code.
- **Engine stage marginal: 0.39–0.71 s.** Ours. This is flatten + statistics + quality window — the same
  stage EXP-0005 cut from 1.649 s to 0.465 s.
- **Artifact write + hash marginal: 0.03–0.12 s.** Content addressing costs little at 20 MiB.

Whole-ingest landed at 0.875–0.990 s (20.4–23.0 MiB/s) in these runs, consistent with EXP-0005's
10.8–18.3 band and above NFR-001(c)'s revised ≥ 10 MiB/s P50 floor in every run.

### cProfile hotspots, full ingest (ordering, not timing — the profiler triples the wall clock)

| Function | cumtime share of profile |
|---|---|
| `mcap/reader.iter_messages` (container) | ~39% |
| `mcap_reader._Topic.observe` → `_run` slot walk (72600 msgs, ~1.08M leaves) | ~19% + ~19% tottime |
| `json.loads` → `json.decoder` | ~15% |
| `_Window.add` statistics sampling (642,600 calls) | ~10% |

### Other engine stages, profiled on the current tree (one trial each)

`quality-30000f` → `analysis.quality.analyze` dominates its own workload (linear, ~0.38 s for 30 000
frames, matching EXP-0007's ~6.0 µs/frame). `validation`, `monitor`, `aggregation-500k`, and the API
endpoint workloads show no engine function above their published profiles; the only surprise is
**`collect_provenance` ≈ 0.22 s per harness invocation** (a Windows WMI hardware query) — harness
overhead, paid once per benchmark run, not engine cost. Anyone reading a cProfile of a benchmark run
should expect it as the top cumulative frame.

## Verdict

**No single addressable hotspot remains above the format floor.** The two biggest costs in whole-ingest
(container iteration, JSON decode) are not ours to optimize, and together they bound what any engine
change can achieve: a zero-cost engine path would land near 34–45 MiB/s on this fixture and stop. The
engine's remaining stage is spread across the slot walk and the statistics loops with no function over
~20% of the profile — the `_dimensions` shape of bottleneck is gone. Optimizing the walk further cannot
change any requirement: NFR-001(c) is met with margin in every run, and the decode-avoiding reader (the
recorded path back to ≥ 50 MB/s) remains a deferred design change, not a tuning one.

Decision: **adopt** as the current attribution. No code change accompanies this record — that is the
finding, not an omission: the profiling criterion's one actionable bottleneck (`_dimensions`) was already
addressed by EXP-0005, and this record is the evidence that profiling the current tree produces no new
one.

## Follow-ups

- The scaled scaling-curve campaign (EXP-0010) reuses this script's stage seams per fixture size.
- A decode-avoiding reader (skip unscoreable channels, or a faster decoder) is the only route to
  materially higher throughput; it needs an ADR and stays deferred.
- `collect_provenance`'s WMI query could be cached per process in the harness if benchmark wall time ever
  matters; not done — harness overhead does not affect measured latencies.
