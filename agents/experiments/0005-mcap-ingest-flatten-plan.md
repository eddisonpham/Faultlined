# EXP-0005: The dimension layout, compiled once

- **Date (UTC):** measured 2026-09-30
- **Author/agent:** benchmark-engineer
- **Status:** accepted (follow-up experiment to EXP-0004, which prescribed this change)
- **Related ADR / requirement:** [ADR 0022](../decisions/0022-mcap-ingest-reader.md), [ADR 0023](../decisions/0023-quality-metrics-honesty.md); NFR-001; backlog B-019
- **Supersedes EXP-0004's baseline measurement** (6.21 MiB/s P50). EXP-0004's cost
  attribution is not superseded - it is what found this lever, and it stays as history.

## Hypothesis / purpose

EXP-0004 measured `_dimensions` at 51% of ingest and named the cause: a dotted path string
(`/joint_states.effort[4]`) built for every numeric leaf of every message - 540 000 strings
for 18 distinct names. Its follow-up section proposed the fix almost verbatim: compile the
first message's paths once and apply the layout by index. B-019 asks whether that closes the
gap, and what the measurement says about the provisional 50 MB/s target.

## Change under test

One behaviour-preserving change and one deliberate behaviour addition:

1. **Compiled flatten plan** (`mcap_reader._compile` / `_run`, `_Topic.observe`). Each topic
   compiles its message shape once - names built once, ever - and per message walks the
   decoded object into a slot buffer with no string building and no dict merging. A shape
   change (new key, reordered keys, resized list, leaf kind flip under a container) is
   detected by the walk itself and falls back to the original dict path, which is kept in
   the code base both as the fallback and as the equivalence oracle. The statistics loops
   lost their per-value `min`/`max` calls to comparisons with identical NaN semantics.
2. **A number `float()` cannot hold is dropped, not fatal.** JSON parses a 400-digit
   integer; the old code raised `OverflowError` and failed the whole episode read over one
   leaf. Same defect class as the sqrt overflow ADR 0023 fixed: the formula is not the
   data. Behaviour on every representable input is unchanged.

Output equivalence is pinned two ways: a full-extraction JSON snapshot of the 20.1 MiB
fixture compared byte-for-byte before and after, and `test_the_plan_path_and_the_dict_path_agree_on_every_message`,
which holds the fast path to bit-identical output against `_observe_dims` (the shipped dict
path) across a 13-message sequence of every hazard found so far: key reorder, ragged
messages, extra names, leaf kind flips, list resize, deep nesting, bool/str/None leaves,
root scalars, root lists, tuples, and an unrepresentable integer.

## Configuration

Workload `mcap-ingest` v1.0.0; 1 warmup; 5 measured trials; 1 worker; same fixture and
same input hash as EXP-0004 (`8973d57b...`, 21 125 545 bytes). The operation is the real
ingest path - sniff, read, describe, content-address, write the artifact - with an
in-memory catalog and the production filesystem artifact store.

## Provenance

| Field | Value |
|---|---|
| git commit | measured immediately before the commit carrying this record; tree under test is that commit's tree |
| git dirty | `true` (documented above; the alternative was recording a hash of code that did not exist yet) |
| dataset | `faultlined so101_pick_place sensor log (MCAP, JSON-encoded topics)` |
| input SHA-256 | `8973d57bc16e93477358d3507331af5230b17605e181b9449d6a16b76e984b3e` (unchanged from EXP-0004) |
| OS / Python / app | Windows 11 10.0.26200 SP0 / 3.14.5 / 0.1.0 |
| hardware | Intel64 Family 6 Model 198 Stepping 2, 24 logical CPUs, 33.75 GB RAM, disk `C:\` |
| background load | None controlled; owner machine interactive |

Committed baseline rewritten: `benchmarks/baselines/mcap-ingest-windows.json`
(`source_run_id` `b0fd1ca5-e305-40a4-9ca8-5442cf435489`).

## Results

Whole-ingest P50, same protocol as EXP-0004, three separate runs:

| Run | P50 | Throughput | vs EXP-0004 |
|---|---|---|---|
| EXP-0004 (before) | 3.247 s | 6.21 MiB/s | 1.0x |
| A | 1.841 s | 10.92 MiB/s | 1.76x |
| B | 1.856 s | 10.83 MiB/s | 1.74x |
| C (baseline write) | 1.102 s | 18.28 MiB/s | 2.94x |

Direct reader on the same file (best of 5 in one process): 1.180 s -> 0.799 s
(17.1 -> 25.2 MiB/s, 1.48x), with the byte-identical output snapshot attached to the run.

Stage attribution, fresh process per pass, median of 3 - the table EXP-0004 made worth
keeping, updated:

| Stage | Time | Equivalent throughput | Marginal cost |
|---|---|---|---|
| MCAP container iteration (`iter_messages` only) | 0.154 s | 131 MiB/s | — |
| + `json.loads` on every message | 0.472 s | 42.6 MiB/s | +0.318 s |
| + flatten plan, statistics, quality window | 0.937 s | 21.5 MiB/s | **+0.465 s** (was +1.649 s for `_dimensions` alone) |

The engine's own stage - everything EXP-0004 attributed to `_dimensions` plus the
statistics and window work around it - fell from 1.649 s to 0.465 s, a 3.5x reduction.
It is no longer the majority of anything: most of what remains between the two middle
rows is JSON decode, which is C code and not ours.

## Verdict against the provisional target

**Still not met** (10.8-18.3 MiB/s against 50 MB/s), and now with a measured reason the
gap cannot close under this design: the floor is the two stages nobody owns. Container
iteration plus `json.loads` alone run at 0.47 s for this file - **42.6 MiB/s before the
engine does any work at all**. A zero-cost engine path would land near 40 MiB/s and stop.
Reaching 50 MB/s on JSON-heavy bags requires decoding less (skipping channels that cannot
be scored) or a faster JSON decoder - both design changes, both needing an ADR and, for
the second, a dependency that must earn its place.

What changed hands since EXP-0004: the target's last 8x was **ours** and is now down to
1.75-2.95x, of which decode is the largest single block. The honest reading of B-019 is
that the named bottleneck is fixed and the target itself needs re-scoping: per-stage
targets (container, decode, engine) would be falsifiable, where one end-to-end number
mixes two libraries and a disk. That is an owner decision and is left open.

Two measurement caveats worth carrying forward:

- **Repeated reads degrade inside one process** (best-of-5 0.799 s vs median-of-3 2.047 s
  on the same code). Memory churn from millions of short-lived objects is the suspect.
  Best-of-N in one process and fresh-process medians disagree by up to 2x; this record
  prefers fresh processes for attribution and reports the harness band for the headline.
- **Five trials on an interactive box give a band, not a number** (P50 1.10-1.86 s across
  runs, 1.6x spread). The before/after ratio is stable across all three runs; the
  absolute value is not. The harness's fixed 5-trial protocol should either grow trials
  or report bands for real-data workloads.

## Follow-ups

- **Re-scope the NFR-001 provisional target** (owner decision): per-stage targets, an
  end-to-end target that names the JSON-heavy path explicitly, or acceptance that a
  `ros2msg` bag (which skips decode of scored channels) is the deployment-relevant case.
- **Measure a real `ros2msg` bag** when one exists (carried from EXP-0004 unchanged).
- **Trial ramp instrumentation** in the harness: record per-trial order so a degradation
  pattern is visible in the result instead of discovered by profiling.
- If 50 MB/s must hold for JSON bags: the next lever is *decoding less*, not walking
  faster - score only channels that carry the quality signal and count the rest from
  framing metadata. Design change; ADR first.
