# EXP-0007: Heavy-load campaign and the metrics-sink tail read (B-013, B-015, B-007)

- **Date:** 2026-09-30
- **Question:** Do latency, throughput, and memory stay sane 10-100x past the
  measured operating point, and does any hot path bend or break at that scale?
- **Method:** three new registry workloads over the existing harness
  (`quality-analysis-30000f`, `metrics-aggregation-500k`, `mcap-ingest-hour`
  over a freshly generated deterministic 3600 s bag, sha256
  `56c53fef...0160051`), plus a code-level optimization the campaign motivated.
- **Results:**
  - `quality-analysis-30000f`: **P50 0.181 s / P95 0.222 s** (n=5). About
    6.0 us/frame/dim-set, matching the ~linear model EXP-0002 established at
    303-3000 frames. No pathology at 100x the standard episode.
  - `metrics-aggregation-500k`: **P50 1.54 s / P95 1.90 s** (n=5) in memory -
    about 324k records/s. B-015's revisit condition (p50 > 100 ms) fires at
    this scale, so the aggregation read path was re-examined.
  - `mcap-ingest-hour`: **P50 13.22 s / P95 13.34 s** for a 42.9 MiB,
    3600 s bag (n=3). About 30 us/message against the 20-minute bag's 26 -
    linear in messages. The lower headline MiB/s vs EXP-0004 (3.2 vs 6.2) is
    the fixed 16 MiB attachment amortizing away, not a slowdown: per-message
    cost is flat across a 3x size change, so no size pathology up to 1 h.
  - **Optimization (motivated, then measured):** `read_metric_records` parsed
    the entire sink on every call even though both callers ask for the newest N
    (the monitor's tick read was full-history forever). It now reads the file
    backward in 1 MiB chunks and stops at N valid records (capped at 64 MiB),
    so a read costs the window, not the history. At the 500k-record size the
    old full parse cost about 30 s of json.loads; the tail read is bounded by
    the requested window by construction, and the boundary behavior (chunk
    splits, corrupt lines, undersized/oversized windows) is pinned by tests.
- **Conclusions:** scaling is linear in every direction measured; the one
  super-linear hazard (full-sink parse per tick/request) is removed at the
  reader, not by compaction - revisit B-015 compaction only if a
  window-sized read itself becomes the bottleneck.
- **Provenance:** clean tree at commit time; host (Windows, Python 3.14);
  harness defaults per workload as registered; raw results under
  `benchmarks/results/` (gitignored by policy).
