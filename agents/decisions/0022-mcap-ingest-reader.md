# 0022. MCAP ingest: one file is one episode, and the file's own words are the only metadata

- **Status:** accepted
- **Date (UTC):** 2026-09-29
- **Deciders:** owner + implementer agent

## Context

[ADR 0006](0006-storage-and-formats.md) chose "MCAP in, LeRobot v3 out" as the storage contract, and
`mcap>=1.2` has been a declared dependency since scaffolding. The reader was never written, which left
the definition of done with one unchecked MVP criterion and the README claiming a capability the
registry did not have. The registry itself promised the fix would be cheap: "adding a format (ROS 2
bag later) means adding one reader + tests - no core changes" (`ingest/readers/registry.py`).

MCAP does not look like a LeRobot dataset, and three of its properties forced real decisions:

1. **There is no episode.** A bag is a set of topics with independent rates and no frame table.
   LeRobot v3 can be sliced by row range; an MCAP file cannot be sliced at all without a convention
   this project would then have to invent and defend.
2. **There is no frame rate.** Joint states publish at 50 Hz, images at 10 Hz, diagnostics at 1 Hz.
   `EpisodeExtraction.fps` feeds the `min_fps`/`max_fps` validation rules, so it has to be *some*
   number and the choice has to be explainable to the operator who gets quarantined by it.
3. **There is no robot or task field.** LeRobot carries `robot_type` in `meta/info.json` and `tasks`
   in the episode index. MCAP has a `Metadata` record type and no standard for what goes in it.

A fourth property is about cost. A bag is the one input whose length is set by the robot, not by the
recording session, and `architecture/storage.md` §1 requires ingest to stay O(one episode's summary)
rather than O(dataset). The LeRobot reader gets that for free because one Parquet file is one
episode; a bag has no such boundary.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Describe the file, choose the two ambiguous facts explicitly (chosen)** | No invented structure; the operator can see which topic produced the frame rate and which produced the quality verdict; ingest stays streaming; reader is ~250 lines and the registry is untouched | A bag is always one episode, so a multi-session bag becomes several ingests; CDR and protobuf payloads are counted but not read into numbers |
| Split a bag into episodes on topic-name or time-gap boundaries | One bag could yield many episodes | Requires a segmentation heuristic with no ground truth to tune it against. A wrong split produces two half-episodes that both pass validation - a silent data-integrity failure, which is the exact class of bug this project exists to prevent |
| Decode every payload format (CDR, protobuf, flatbuffers) via the schema | Full statistics and quality on real ROS 2 bags | A struct-layout decoder per encoding, each a guess validated by nothing. A wrong number in the catalog is worse than a documented gap, and `ros2msg` alone is a large piece of work with its own test matrix |
| Buffer the whole bag, then analyse | Simplest quality computation | Restores the O(dataset) ingest the storage design forbids; peak memory scales with log length, so a 6-hour session is a worker OOM (F5) |
| Defer MCAP again and describe the gap honestly | No new code | Leaves the chosen input format unimplemented, the README claim wrong, and B-002 unmeasurable. The dependency was already declared and paid for |

## Decision

Add `McapReader` as a second entry in `READERS`. No core change: `read_episode`, the ingest service,
validation, the catalog and the API are untouched, which is the promise the registry module makes and
this is the test of it.

**A file is one episode, and the key is the filename.** `episode_key` is `file=<name>`. A caller that
supplies a different key gets an error naming the key the file actually has, rather than the first
topic's contents being returned under a name that does not describe them. This is the same position
`LeRobotReader._select` takes on an out-of-range index.

**Frame rate is the busiest topic's rate, measured as intervals.** `fps` is
`(messages - 1) / span` for the highest-message topic, ties broken by topic name. The `-1` is
load-bearing: `messages / span` reports 50.05 Hz for a stream that is 50 Hz, because it counts a
period that never elapsed, and a validation rule bounded at 50 Hz would then quarantine correct data.
The topic that produced it is published as `dataset.fps_source_topic`, because a frame rate with no
stated origin is not reviewable.

**Motion quality is read from the busiest topic with more than one dimension.** Ties break on message
count, then dimension count, then name. The first version picked the busiest topic outright, and on
the project's own generated log that selected a 1-dimension gripper command - which `analyze`
excludes from the verdict by design (discrete and gripper dimensions never judge motion, ADR 0018),
so it reported `unknown` on a log that plainly contained an 18-dimension joint stream. The
selection rule is therefore stated in terms of what the analyser can actually use, not in terms of
what is merely largest.

**Only `json` payloads are interpreted.** CDR (`ros2msg`) and protobuf channels are counted, timed and
named, but contribute no numbers. This is a real limitation and it is a deliberate one: a
hand-written struct decoder for an encoding we have no fixture for would place plausible, wrong
numbers in the catalog. `dataset.undecodable_channels` names them, so the gap is visible on the
episode rather than looking like a channel with no statistics.

**Robot and task come from `Metadata` records, first value wins.** A bag declares them as
`robot_type` and `task` keys of any `Metadata` record; the first record in file order to carry a key
wins, so a file with two records declaring the same key reads the same way every time. A bag that
declares neither yields `unknown` / `""`, and the existing `UNDECLARED_TASK` rule quarantines it -
which is the correct outcome, not a reader failure.

**Statistics are exact and streaming; quality is sampled and bounded.** Per-channel min/max/mean/std
are running accumulators over the decoded numbers, so they describe the whole log from constant
memory. The quality sample is a per-dimension window of at most `2 x QUALITY_WINDOW` values that
halves itself and doubles its stride whenever it overflows. Halving rather than keeping the first N
is what keeps the sample spread across the whole log: a first-N window would describe the opening
seconds of a six-hour recording and call it the episode.

## Consequences

- (+) The two open MVP criteria are closable with evidence rather than with a caveat: MCAP ingest runs
  through the queue, worker, artifact store, catalog, validation and build path, and backlog B-002
  finally has a real reader to measure.
- (+) A ROS 2 bag ingested today still gets exact channel presence, message counts, duration and a
  frame rate, so `required_channels`, `frame_count` and `duration` rules work on it. Only the
  per-value statistics and the motion verdict are missing, and both say so.
- (+) The registry's "one reader plus tests" claim is now tested rather than asserted: the
  dispatch test and the "tried: mcap, lerobot" error message both go through the same table.
- (+) `scripts/make_mcap_log.py` is a deterministic, closed-form generator, so the reader's unit tests
  and the B-002 workload measure the same bytes. A benchmark that regenerated its input per trial
  would have measured `mcap.writer`.
- (−) A multi-session bag is N ingests, not one. This is the cost of not inventing a segmentation
  heuristic, and it is the right trade for MVP; the heuristic becomes defensible only when there is
  labelled ground truth to tune it against.
- (−) Quality numbers on a bag come from a decimated sample, so `movement_score` and `jerk_score` on a
  40-minute recording are computed from ~1024 frames, not all of them. Channel statistics are not
  sampled and are exact. The distinction is asserted by tests on both sides.
- (−) The quality and frame-rate rules are conventions, not facts from the format. They are documented
  in the reader, published per episode as `fps_source_topic` / `quality_source_topic`, and stable
  across runs - but a different bag could reasonably want different ones, and that is a future
  profile-level decision rather than a reader change.

## Addendum (2026-09-29): the window size is a claim about memory, so it is tested as one

`QUALITY_WINDOW = 512` is not a tuning constant chosen for accuracy. It is the bound the reader
promises about its own peak memory, so the test asserts the *invariant* - that the retained sample
stays inside `(QUALITY_WINDOW, 2 * QUALITY_WINDOW]` for a log twenty times longer than the window -
rather than a specific count. A test that pinned the number would have passed while the property it
was written to protect had been broken by a refactor of the decimation.
