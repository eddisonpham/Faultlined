# EXP-0004: MCAP ingest throughput, and where the time actually goes

- **Date (UTC):** measured 2026-09-29
- **Author/agent:** benchmark-engineer
- **Status:** accepted (first real-format ingest baseline; owner authorized 2026-09-29)
- **Related ADR / requirement:** [ADR 0022](../decisions/0022-mcap-ingest-reader.md); FR-001, NFR-001; backlog B-002
- **Supersedes nothing.** EXP-0001 remains the micro-benchmark reference; this is a different workload over a different input.

## Hypothesis / purpose

B-002 asks whether one worker can sustain the provisional 50 MB/s target on local NVMe for realistic
robot episodes, and this is the first measurement taken against a real MCAP file rather than a synthetic
payload. The secondary question matters as much as the number: **if ingest is slow, which stage is
slow**, because "the reader is slow" is not an actionable finding and "we build one f-string per
numeric leaf" is.

## Change under test

No optimization. This records the current `mcap-ingest` workload as the reference point, and
attributes its cost across the pipeline stages.

## Configuration

Workload `mcap-ingest` v1.0.0; 1 warmup; 5 measured trials; 1 worker. Input is a deterministic bag
from `scripts/make_mcap_log.py --seconds 600 --attachment-mib 16`: a 10-minute recording, five
JSON-encoded topics (30 000 joint-state messages at 50 Hz, 30 000 gripper commands, 6 000 TF, 6 000
image metadata, 600 diagnostics) and one 16 MiB binary camera attachment, zstd-chunked. The operation
is the real ingest path - sniff, read, describe, content-address and write the artifact - with an
in-memory catalog so the number is the engine's own cost and not Postgres latency.

**The attachment is deliberate.** A metadata-only bag is the worst case for a reader that decodes
JSON, and an attachment-heavy one is what a real episode looks like. Measuring only one of the two
would make the result unfalsifiable in whichever direction it came out.

## Provenance

| Field | Value |
|---|---|
| git commit | `a6a3ef2` (reader, generator, workload) |
| git dirty | `false` |
| dataset | `faultlined so101_pick_place sensor log (MCAP, JSON-encoded topics)` |
| input SHA-256 | `8973d57bc16e93477358d3507331af5230b17605e181b9449d6a16b76e984b3e` |
| input size | 21 125 545 bytes (20.1 MiB) |
| OS / Python / app | Windows 11 10.0.26200 SP0 / 3.14.5 / 0.1.0 |
| hardware | Intel64 Family 6 Model 198 Stepping 2, 24 logical CPUs, 33.75 GB RAM, disk `C:\` |
| GPU | NVIDIA (NVML), 8.55 GB, present but unused by this workload |
| background load | None controlled; owner machine interactive |

Committed baseline: `benchmarks/baselines/mcap-ingest-windows.json` (schema v1, `source_run_id`
`a5723e9b-23d8-4a6c-90e3-27b5e65f2753`). The raw schema-v2 result is gitignored under
`benchmarks/results/`, so the `source_run_id` resolves only on the measuring machine.

## Results

| Metric | Value |
|---|---|
| P50 | 3.2469 s |
| P95 / P99 | 3.3185 s / 3.3185 s |
| Mean | 2.9312 s |
| Std dev | 0.7405 s |
| 95% CI of mean | 2.2644 – 3.2921 s |
| Throughput (P50) | **6.21 MiB/s** |
| Failures | 0 / 5 |

## Cost attribution

Median of 3 passes over the same file, isolating one stage at a time. This is the part of the run
that is worth keeping.

| Stage | Time | Equivalent throughput | Marginal cost |
|---|---|---|---|
| MCAP container iteration (`iter_messages` only) | 0.219 s | 92.2 MiB/s | — |
| + `json.loads` on every message | 0.346 s | 58.3 MiB/s | +0.127 s |
| + `_dimensions` flatten of `/joint_states` | 1.995 s | 10.1 MiB/s | **+1.649 s** |
| Full ingest (above + artifact write + catalog + quality) | 3.247 s | 6.21 MiB/s | +1.252 s |

Three things follow, and none of them are what the headline number alone would have suggested:

1. **The container is not the limit.** Reading the MCAP stream alone runs at 92 MiB/s. Neither the
   zstd chunks nor the record framing are what stands between this workload and the 50 MB/s target.
2. **The cost is ours, and it is one function.** `_dimensions` builds a dotted path string
   (`/joint_states.effort[4]`) for every numeric leaf of every message - 540 000 strings for this
   log - and then merges the per-level dicts back together. It is **51% of total ingest time** and
   roughly 8× the cost of the JSON parse it operates on. The names are what make the quality verdict
   legible, and they are being rebuilt from scratch 540 000 times to produce 18 distinct strings.
3. **The remaining 1.25 s is not the artifact store.** Content-addressing and writing 20.1 MiB costs
   ~65 ms. A number that says "ingest is slow" and attributes it to disk would have been wrong by an
   order of magnitude.

## Verdict against the provisional target

**Not met, by roughly 8×** (6.2 MiB/s against 50 MB/s). Stated plainly because the target is
provisional and was set before any MCAP reader existed.

What the measurement does support:

- The design decisions in ADR 0022 hold. Peak memory was bounded by topic count and the quality
  window, so a 10-minute log and a 10-hour log cost the same memory; the number here is time, not
  memory, and the two were not conflated.
- Content-addressed ingest of a real bag works end to end and is reproducible against a fixed input
  hash.
- The 50 MB/s target is plausible for a reader that does not rebuild constant strings per leaf. It is
  not plausible for this one, and nothing about the format or the hardware argues against it.

What it does not support: any claim about ROS 2 bags in production. This fixture's payloads are JSON
by construction, and ADR 0022 deliberately does not decode CDR or protobuf - a bag whose channels are
`ros2msg` would skip the `_dimensions` stage entirely and land much closer to the 58 MiB/s decode
line. **The JSON-heavy path is the slow path, and a real ROS 2 deployment may never take it.** That
makes this the pessimistic bound for the format and the optimistic one for the code.

## Trade-offs

The alternative was to optimize first and record the better number. Rejected: an unmeasured baseline
is a target, and the project already committed to measuring before optimizing
(`agents/benchmarking/methodology.md`). Recording 6.2 MiB/s with a cost breakdown is worth more than
recording an unexplained improvement to it.

## Follow-ups

- **Cache the dimension layout.** The first decodable message already fixes the dimension set
  (`_Topic._sample`); `_dimensions` could compile that message's paths once into a flat plan and then
  apply it by index. This is the obvious next experiment, expected to be the single largest win
  available, and it is a change to one function with an existing exact-statistics test to hold it
  honest.
- **Re-measure B-002 after that change** and supersede this record rather than editing it.
- **Measure a `ros2msg` bag** once a real one is available, to establish how much of this number is
  the JSON path specifically. Until then the 58.3 MiB/s decode line is a bound, not a result.
- The 5-trial sample has a ±0.74 s standard deviation on a 3.25 s median, which is wide. The
  attribution above is the load-bearing result; the headline throughput is a reference point, not a
  precise constant.
