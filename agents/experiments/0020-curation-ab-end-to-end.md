# EXP-0020: Does the redundancy report let an operator keep the behaviours at a fraction of the export?

- **Date (UTC):** 2026-10-07
- **Author/agent:** Buffy (implementer/benchmark engineer), played as an NVIDIA/Tesla platform
  engineer using the product the way an operator would, curation improvement programme
- **Status:** done
- **Related:** [ADR 0032](../decisions/0032-behavioural-fingerprints.md), [EXP-0019](0019-behavioural-fingerprint-calibration.md),
  [the improvement ranking](../research/2026-10-07-curation-idea-improvements.md) idea C1,
  [benchmarking methodology](../benchmarking/methodology.md)
- **Harness:** `scripts/fingerprint_ab.py` (drives the HTTP API only; writes no repository state)

## Hypothesis / purpose

EXP-0019 calibrated the fingerprint in isolation and measured it through the repository's own
reader. That is not the same claim as the one the feature exists to make. The claim an operator
cares about is end to end:

> Given a catalog that holds the same behaviour recorded several times, can the report tell me
> which episodes to keep, so that a build of the *representatives* exports materially less data
> while still holding every distinct behaviour?

This is deliberately a different question from precision/recall. A report can be precise and still
useless (if it finds nothing), and it can find everything and still be useless (if the operator
cannot act on it). The end-to-end question can only be answered by walking the API a human walks.

## Change under test

One primary variable: **which episodes the build contains.**

- **Arm A (control):** every validated episode.
- **Arm B (treatment):** the report's representatives only.

Everything else is held fixed: same corpus, same validate profile, same build/export path, same
catalog, run back to back in one process.

## Configuration

- Corpus: **24 distinct behaviours × 4 takes = 96 episodes**, planted by the same generator as the
  calibration harness (`_series`: jitter + a ~2 % amplitude and phase shift per take).
- Every step goes through the product's own door: `POST /api/v1/jobs` with `ingest`, `validate`,
  `build` and `export`, and `GET /api/v1/builds/{hash}/redundancy` for the report. Nothing is
  computed twice or out of band; no test client, no direct catalog access.
- Throwaway database `bb_ab` on the isolated cluster (port 55432); `DE_METRICS_PATH`,
  `DE_ARTIFACT_ROOT` and `DE_EXPORT_ROOT` under a scratch directory; the server started with
  `de dev` (the documented deployment).
- While it ran, the harness polled `GET /api/v1/monitoring/health` after each job and called
  `POST /api/v1/monitoring/tick` once, so the platform's own telemetry and detector were exercised
  rather than assumed.

## Provenance

Commit `4c4e2f2` (the feature), harness `scripts/fingerprint_ab.py`, seed 20261007, drive-by
`--behaviours 24 --copies 4`, `DEFAULT_THRESHOLD = 0.04`, `MAX_EPISODES = 500`. One run.

## Results

| | arm A (all episodes) | arm B (representatives) |
|---|---|---|
| episodes built | 96 | **24** |
| behaviours planted | 24 | 24 |
| build + export | 3.4 s | 1.9 s |
| export bytes | 1 315 547 | **330 022** |
| export files | 195 | 51 |
| export job state | `succeeded` | `succeeded` |

- **Planting retained: 24 episodes for 24 behaviours = 1.00×.** No behaviour was collapsed away —
  which is the whole risk of a dedup step, and it did not happen.
- **Arm B's export is 25 % of arm A's** — 4× smaller for identical behavioural coverage.
- The report, as the page asks for it: **96 scored, 24 distinct, 72 near-duplicate, 75 % redundant,
  224 ms, `truncated=False`.** (96 episodes is well inside `MAX_EPISODES`, so nothing was sampled.)
- Ingest: 96 episodes in 90.6 s (**944 ms/episode**), same order as EXP-0001's synthetic baseline.
- Validate: **succeeded in 12.2 s, 96 of 96 valid.**
- Monitoring: **99 samples, 0 unreachable**, `blind` values `{'False'}`, `last_tick_at` present,
  73 metric summaries in the 1 h window. `POST /api/v1/monitoring/tick` returned
  `observed_at 2026-10-07T06:12:54+00:00`, `catalog_reachable: true`, `blind: false`.
- Wall clock: 109.2 s.

## Trade-offs

- The reduction here is **4× because the corpus says so**: 4 takes per behaviour is a synthetic
  ratio, chosen to be answerable rather than realistic. A real queue with 1.2 takes per behaviour
  would show ~17 %. The number that transfers is not "75 %"; it is **"every planted behaviour
  survived"**, which was measured rather than assumed.
- Arm B is faster (1.9 s vs 3.4 s) only because it moves a quarter of the bytes. The report itself
  costs 224 ms, which arm A does not pay.
- Both arms were built and exported, so the run is not blinded to arm A's cost by caching.

## Conclusion

**Decision: adopt — the feature's end-to-end claim holds.** Given a catalog with repeated
recordings, the report produced a representative set that exported 4× smaller while retaining
100 % of the planted behaviours, through the same API a human uses, with the platform's own
monitoring green throughout.

**The first attempt failed, on a defect this programme had already found and not fixed**, and that
is worth recording as a result of its own:

- A validate job with the default `profile: {}` **passed the request schema and then settled
  `failed` in 0.6 s with `0 of 96 valid`**, and the arm-A export that followed was refused with a
  422 because the build it referenced never existed. The profile document requires a non-empty
  `name` and `version`; the empty document the schema defaults to is rejected by the handler, not
  by the schema, and the failure surfaces as a parse-failure code. This is open issue (1) from the
  2026-10-06 whole-program assessment, reproduced live — **a real operator's first action fails,
  and the error tells them nothing about why.**
- The harness works around it with an explicit profile and documents the trap at the top of the
  file, so the driver does not hide the defect; it routes around it on purpose.

## Limitations

- **One run, n=1.** No repeats, so no p50/p95 — the cost half of this experiment is a single
  observation and is reported as such. EXP-0019 carries the repeated measurements.
- One corpus generator, one threshold, one build size. The behaviour-loss result (1.00×) is the
  meaningful one and it is a single draw; it should be repeated across seeds before the feature is
  trusted as a default-on step in an operator's loop.
- The harness counts absolute state and refuses a populated catalog, so this is not yet a
  repeatable CI check — it is a deliberate end-to-end drill.
- Export size is read off the disk, not from the export job's own report; a build's members were
  not cross-checked against the export's contents beyond the file count.

## Follow-ups

1. **Fix the empty-profile trap** (open issue 1). A schema that accepts what the handler refuses is
   the same class of defect as a route that accepts a job type it cannot run, and it is now
   reproduced twice.
2. **Repeat the A/B across seeds** and record the distribution of behaviours retained, so "1.00×"
   becomes a claim with a spread rather than a single draw.
3. **C2: build coverage.** Arm B's build is now *narrower* than arm A's; nothing tells an operator
   which tasks, robots, formats or verdicts the smaller build gave up. The next slice.
4. Wire "redundant" into slices as a filter, so the report can be acted on where curation happens
   rather than only at build time.
