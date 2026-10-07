# EXP-0021: Build coverage — is it bounded, is it cheap, and does it tell an operator anything?

- **Date (UTC):** 2026-10-07
- **Author/agent:** Buffy (implementer/benchmark engineer), played as an NVIDIA/Tesla platform
  engineer using the product the way an operator would, curation improvement programme
- **Status:** done
- **Related:** [ADR 0033](../decisions/0033-build-coverage.md), [EXP-0020](0020-curation-ab-end-to-end.md),
  [EXP-0019](0019-behavioural-fingerprint-calibration.md),
  [EXP-0015](0015-feature-latency-at-three-scales.md) (the 2.2 MiB aggregate this one is measured against),
  [round-2 research](../research/2026-10-07-dataset-coverage-and-diversity.md),
  [benchmarking methodology](../benchmarking/methodology.md)
- **Harnesses:** `scripts/coverage_bench.py` (cost and payload at scale) and
  `scripts/fingerprint_ab.py` (extended with the coverage leg; drives the HTTP API only). Neither
  writes repository state.

## Hypothesis / purpose

ADR 0033 claims three things that can each be checked, and one of them is the reason the idea was
worth building at all:

1. **Bounded.** The payload is bounded by the axes and their caps, not by the episode count.
2. **Cheap.** The read plus the fold stays inside the 200 ms read target at 10 k episodes.
3. **Informative.** The gap list is not empty and not "everything is covered" on a build that visibly
   holds a fraction of its catalog.

The falsifier was written into the ADR and into the harness *before* the numbers existed: a payload
that grows with the episode count, a read over 200 ms at 10 k, or a vacuous gap list would each have
dropped the idea rather than produced a polish task.

A fourth question belongs to the curation programme rather than to the ADR, and is answered by the
second half of this record: **does the coverage report independently confirm what the A/B in EXP-0020
claimed?** EXP-0020's arm B is a narrower build whose retained-behaviour count is asserted from the
fingerprint's own partition — the same descriptor that chose the representatives. A second opinion
from a *different* source (the catalog's persisted task/robot/format/verdict columns) is what makes
"no behaviour was lost" a measurement rather than a restatement.

## Change under test

One feature: `GET /api/v1/builds/{hash}/coverage` and the `Coverage` section on `/ui/builds/{hash}`,
backed by `PostgresCatalog.build_coverage_inputs()` and the pure `analysis/coverage.py`.
`VALUE_LIMIT = 20`, `GAP_LIMIT = 20`.

## Configuration

**Cost and payload — `scripts/coverage_bench.py`, throwaway database `bb_coverage`:**

- Corpus: **10 000 episodes**, 200 distinct task labels (each mapped to its own vocabulary entry)
  **plus 12 labels with no episode anywhere**, 4 embodiments, 3 formats, 3 verdicts, and one episode
  in ten deliberately left unscored. Rows for a prefix of that catalog are built at 100, 1 000 and
  10 000 episodes, so each size is a real content-addressed build over a real slice.
- 9 repeats per size; median and max reported. A connection floor (one repository call, its own
  connection) is measured in the same process, because every repository call opens its own connection
  and on this host that is tens of milliseconds — the difference between "the query is slow" and
  "every route on this host pays this".
- Row seeding uses the writer's own statements on one connection (documented in the harness): the
  per-row `register_episode` path costs three connections per episode, i.e. ~30 minutes for 10 k
  episodes, which would measure Windows rather than the report. **The read path under test is
  untouched.**

**What it sees — `scripts/fingerprint_ab.py`, throwaway database `bb_coverage_ab`:** the EXP-0020
drill, with the coverage leg added. 24 behaviours × 4 takes = 96 episodes through
`POST /api/v1/jobs` (ingest, validate, build, export); after both arms are built, one
`GET /api/v1/builds/{hash}/coverage` per arm. Server started with `de dev`; monitoring polled after
every job.

## Provenance

This record ships with the change it measures (ADR 0033 + `analysis/coverage.py` + the repository
method + the route and page section + both harnesses). Seeds: bench corpus deterministic, driver seed
20261007. One run of the A/B (n=1); 9 repeats per size in the bench.

## Results — cost and payload

A repository call on this host costs **48.6 ms**, and a coverage read opens two of them (values, then
the vocabulary), so ~97 ms of the read is connection setup.

| episodes | read p50 | read max | aggregation p50\* | fold p50 | total p50 | rows | payload | tasks present | gaps shown/total | truncated axes |
|---|---|---|---|---|---|---|---|---|---|---|
| 100 | 153.6 ms | 373.4 ms | 56.3 ms | 0.26 ms | 153.8 ms | 31 | 2 221 B | 20 | 20/112 | 1 |
| 1 000 | 93.5 ms | 221.2 ms | −3.8 ms | 0.27 ms | 93.8 ms | 31 | 2 261 B | 20 | 12/12 | 1 |
| 10 000 | 125.7 ms | 166.4 ms | 28.4 ms | 0.34 ms | **126.0 ms** | **31** | **2 296 B** | 20 | 12/12 | 1 |
| 10 000 (whole catalog) | 140.1 ms | 334.8 ms | 42.8 ms | 0.27 ms | 140.4 ms | 31 | 2 296 B | 20 | 12/12 | 1 |

\* read minus two connection floors: an estimate of the aggregation's own cost, not a measurement of
it alone. It is negative at 1 000 episodes because the floor is a median of a noisy distribution —
the aggregation there fitted inside the setup jitter.

- **Falsifier, all three clauses at 10 000 episodes: MET.** Payload bounded — 2 221 B → 2 296 B =
  **1.03×** from 100 to 10 000 episodes, and the growth is the digits of the counts, not data. Gaps
  non-empty — 12 named tasks with no episode anywhere. Read plus fold under 200 ms — **126.0 ms**.
- **The payload is 1 000× smaller than the aggregate it was measured against**: `quality/summary`
  ships **2.2 MiB** at 10 k episodes (EXP-0015) and this ships **2.2 KiB** at the same scale. Rows
  returned by the query are **31 at every size** — the bound is the axes, exactly as decision 5 says.
- **The pure fold is free**: 0.26–0.34 ms at every size, i.e. the report is not arithmetic.
- The 100-episode row is *slower* than the 1 000-episode row (153.6 vs 93.5 ms) and the two
  measurements of the same 10 000-episode build differ by 14 ms (125.7 vs 140.1 ms). **Sub-20 ms
  differences here are not signal** — they are connection jitter, and the read is dominated by it.
- The half-of-catalog build at 100 episodes is the case that exercises the cap honestly: 112 gaps
  existed and 20 were listed, with `missing = 112` and `truncated = true` beside them.
- Corpus build for the whole catalog: 10 000 episodes seeded and 212 vocabulary entries created in
  40.5 s.

## Results — what the report sees on a real A/B

| | arm A (all episodes) | arm B (representatives) |
|---|---|---|
| episodes | 96 | **24** |
| share of the catalog | 100 % | **25 %** |
| distinct values across the four axes | 28 | **28** |
| task axis | 24 distinct, ×4 each | 24 distinct, ×1 each |
| verdict axis | `moderate` 68, `jerky` 28 | `moderate` 17, `jerky` 7 |
| coverage payload | 2 308 B | **2 308 B** |
| coverage latency | 242 ms | 114 ms |
| export | 1 315 547 B / 195 files | 330 030 B / 51 files |

- **No axis lost a value: arm B holds 28 distinct values on the four axes, the same 28 as arm A.**
  Task 24 → 24, embodiment 1 → 1, format 1 → 1, verdict 2 → 2. The compaction cost no coverage, and
  it is a second source saying so: the fingerprint's partition chose the episodes, and this reads the
  catalog's own columns.
- **Every axis's *distribution* is identical between the arms too** — `behaviour 0` is 4 of 96 in arm
  A and 1 of 24 in arm B, i.e. 4.2 % both times. The only things that moved are `episode_count` (96 →
  24) and `coverage_ratio` (100 % → 25 %). This is the honest shape of the result: **coverage is a
  guard on a curation step, not the detector of its saving.** A report that could see the saving
  would have to see within-task redundancy, which is the fingerprint's job (ADR 0032) — the two
  reports are complementary by construction, and neither replaces the other.
- **Coverage is the cheaper of the two reports**: 242 ms / 114 ms here against the redundancy report's
  **337 ms** for the same 96 fingerprints in the same run (EXP-0020 measured 224 ms on its own run —
  same band, and the difference between the two runs' redundancy numbers is the same connection
  jitter).
- The rest of the drill, unchanged from EXP-0020: ingest **92.3 s for 96 episodes (962 ms/episode)**,
  validate **13.0 s, 96 of 96 valid**, monitoring **99 samples, 0 unreachable**, `blind: false`,
  `last_tick_at` present, **76 metric summaries**, wall clock **113.7 s**, both exports `succeeded`.

## Trade-offs

- **The cost claim is about a host whose every catalog read opens a connection.** At 10 000 episodes
  the report's own work is ~28 ms of query and 0.34 ms of fold; the other ~97 ms is two `connect()`
  calls. The falsifier is met, but the margin is the platform's, not the feature's: on a host with a
  connection pool this route would be ~30 ms and the same route's *relative* cost would be the story.
  Recorded as a benchmark backlog item rather than quietly optimised here.
- **A connection-heavy read also means the two-statement design is worth revisiting**: the values
  statement and the vocabulary statement are ~50 ms apart in setup cost, and folding the vocabulary
  aggregate into the same statement as a JSON column would halve it. Not done here: the falsifier is
  already met, and changing the query after measuring it without re-measuring the alternative is how
  the measurement stops meaning anything.
- The bench corpus is synthetic and plant-shaped: 200 task labels with 50 episodes each is a uniform
  distribution, not an operator's, and the vocabulary is per-label rather than merged. The payload
  bound does not depend on the distribution (it is `axes × limits`), but the *number of gaps* does.
- One run of the A/B. The coverage half of it is deterministic given the same arms, so it transfers
  better than the timings; the timings are a single observation each and are written as such.

## Conclusion

**Decision: adopt — all three falsifier clauses are MET, and the report says something on real
builds.** The payload is bounded at the axis caps (31 rows and 2.2 KiB at 100, 1 000 and 10 000
episodes, 1 000× smaller than `quality/summary`'s aggregate at the same scale), the read plus fold is
126 ms at 10 000 episodes inside a 200 ms target, and the gap list names the 12 tasks the operator
declared and never recorded.

On the curation programme: **coverage independently confirms EXP-0020's claim** — the representative
build holds the same 28 distinct axis values as the control at 25 % of the episodes and 25 % of the
bytes — while showing plainly that it is a guard rather than the thing that finds the saving.

## Limitations

- **The vocabulary reference was not exercised end to end, and its absence has a consequence.** The
  A/B driver never creates vocabulary entries, so its readout is `0 of 0 tasks absent` while the
  catalog plainly holds 24 tasks — i.e. with an empty vocabulary the task axis reports the values a
  build holds and never a gap, because ADR 0033 decision 4 draws task gaps from the vocabulary
  rather than from the catalog. That is the intended trade (it is what makes an unfilled label a
  gap) and the run is the first evidence of its cost. Only the bench (12 unfilled labels, one of them
  with no episode anywhere) proves the vocabulary branch, and only the bench shows a capped gap list
  (20 of 112). The drill should create one unfilled label so the branch is exercised where operators
  actually are.
- No measurement at 100 k episodes. The bound is structural, but "structural" is an argument, not a
  number, and EXP-0010c/EXP-0015 set a precedent for measuring the next decade before claiming it.
- The vocabulary statement's own cost is inside the read number but was not isolated: the two
  statements were not timed separately.
- The bench cannot see the *writer*: seeding bypasses `register_episode`, so a defect in the writer's
  row shape would not show up here (it does show up in the integration tests and in the A/B).

## Follow-ups

1. **Halve the connections per coverage read** (fold the vocabulary aggregate into the values
   statement): ~50 ms of the 126 ms at 10 000 episodes, measured, and the fix is a query change
   followed by a re-run of `coverage_bench.py`.
2. **Create one unfilled vocabulary label in `fingerprint_ab.py`** so the task-gap readout is
   non-vacuous in the end-to-end drill.
3. **B-022: the connection-per-call tax.** Every repository call opens its own connection (~48.6 ms
   measured here); it is now visible in three experiments (EXP-0020's 337 ms redundancy report, 242 ms
   coverage, ~960 ms/episode ingest) and is the platform's largest single latency tax. Measure a
   pooled or reused connection against this record's floor before touching any query plan.
4. **Cross-axis cells** (task × robot and the empty cells between them) were deferred in ADR 0033 for
   payload reasons; the payload is now measured at 2.2 KiB against a 200 ms budget, so there is room
   for a bounded version of C4.
