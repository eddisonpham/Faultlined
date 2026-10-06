# ADR 0018: Episode quality signals are summarized at ingest

- **Status:** accepted, 2026-09-29
- **Deciders:** owner + implementer
- **Supersedes:** none. Complements [ADR 0016](0016-json-validation-profiles.md) (validity) and the
  reader/storage boundary in [ADR 0006](0006-storage-and-formats.md).

## Context

Curation is the platform's thesis, and curation needs *motion quality*, not just validity: a dataset
engine should surface jerky, stalled, or outlier-length episodes before they reach a training set. The
closest production system, `huggingface/lerobot-dataset-visualizer` (source-log #46), ships exactly these
signals (movement score, normalized jerk, motion stalls, length outliers) and its users export flagged
episode IDs as removal commands. Faultlined's reader boundary says the extraction "never holds the
episode's frames in memory", and re-reading bytes later would violate the no-re-read principle used for
validation. Length outliers additionally depend on the whole episode population, which only the catalog
sees.

## Decision

1. **A dependency-free analysis module** (`data_engine/analysis/quality.py`) computes signals from named
   per-dimension frame series: movement score (mean L2 frame delta), jerk score (mean |Δ| normalized by
   each dim's range, over active dims), stall ratio (fraction of flat transitions), and per-dim
   activity/discreteness flags. Formulas are the visualizer's verbatim (activity: p95(|Δ|) ≥ 0.1% of
   range; discrete: ≤ 4 unique values).
2. **Summaries are computed while the rows are in memory**, at ingest: the LeRobot reader attaches
   `EpisodeExtraction.quality` and the synthetic path analyzes its payload; both persist through
   `record_episode_quality`. No frames are retained; the catalog keeps the summary.
3. **Verdict uses absolute normalized-σ bands** (<0.02 smooth, <0.1 moderate, ≥0.1 jerky) over judged
   dims (active, non-discrete, non-gripper). *Deviation from the source:* its bands are relative to the
   roughest dim, which is degenerate — the max dim always lands in the "jerky" bucket, making "Smooth"
   nearly unreachable. Gripper dims (name matches `grip`) are excluded from judgment because binary
   open/close is jerky by nature. Recorded as a correction in source-log #46.
4. **`episode_quality` table**, one row per episode (episode PK → `episodes`), scalars + per-dim JSON.
   Re-ingest of identical content replaces the row (content addressing makes recomputation idempotent).
5. **Length z-scores are computed at read time** against the current population (`get_episode_quality`,
   `quality_summary`), never stored: episode lengths do not change but the comparison population grows.
6. **API**: `GET /api/v1/episodes/{id}/quality` and `GET /api/v1/quality/summary` (length histogram,
   speed distribution, cross-episode per-dim σ matrix, top jerk/stall/length outliers).

## Consequences

- Quality is episode-intrinsic and cheap; dataset-relative views (outliers, matrices) are assembled
  from rows at query time, which is fine at local scale and keeps ingest O(one episode).
- Signals are fps-dependent (deltas per frame); consumers comparing datasets across fps should scale by
  `duration_seconds`/`frame_count` from episode metadata. Documented in the module docstring.
- The verdict is deliberately simple and explainable; if curation needs calibrated thresholds, the
  bands become profile-driven in a superseding ADR (tying into ADR 0016 profiles).
- Episodes ingested before this ADR have no quality row; the endpoints 404/empty rather than re-reading
  bytes. A backfill would need a byte re-read and is deferred until an operator asks.

## Amendment (2026-10-06): the gripper exclusion reads the dimension's field, not its address

The "non-gripper" clause above excluded any dimension whose *name* matched `grip`. The MCAP reader
names dimensions `<topic>.<path>`, so a bimanual rig whose busiest joint topic is
`/left/gripper/joint_states` had **every** joint dimension excluded at once: `judged_dims` fell to
zero and the episode's motion verdict was reported `unknown` even though nothing was wrong with the
data. Byte-identical payloads came out `smooth` under `/joint_states` and `unknown` under
`/left/gripper/joint_states` — a quality verdict that is a function of a topic-naming convention
(EXP-0014 D1).

The exclusion now applies to the dimension's own field name only: the last dotted segment of the
name (`analysis/quality.py::_field`). `...joint_states.position[0]` is classified by `position[0]`;
a payload that literally names a field `gripper` still is one. Nothing about the *bands*, the median
rule or the reported fields changed, and the binary gripper the clause exists for is still caught by
the discrete-dimension rule (ADR 0023). Regression tests:
`tests/unit/test_quality.py::test_topic_name_does_not_decide_the_verdict` and
`::test_a_field_named_gripper_is_still_a_gripper`.
