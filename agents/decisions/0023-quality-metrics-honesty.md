# 0023. Quality metrics: refuse what cannot be measured, judge by majority, and score time

- **Status:** accepted
- **Date (UTC):** 2026-09-29
- **Deciders:** owner + implementer agent
- **Amends:** [ADR 0018](0018-episode-quality-signals.md) (quality signals), decisions 1, 3 and 5.
  The original text stands as written; this record states what changed and why.

## Context

ADR 0018 defined the motion-quality signals by following
`huggingface/lerobot-dataset-visualizer` (source-log #46) and corrected one of its choices (absolute
rather than relative verdict bands). It was written from the formulas, and the formulas had only ever
been exercised on synthetic series. An adversarial probe (`analysis` run of 2026-09-29, four defects
below) fed the analyzer the inputs real hardware eventually produces and found that four of its
guarantees did not hold.

The probe was not adversarial for its own sake. Every finding is something a physical robot does
within a week of recording: an encoder that reports `NaN` once, a coordinate that accumulates past
1e154, a bag writer that stalls, one miscalibrated joint out of eighteen.

1. **A single `NaN` moved every population statistic in the product.** `analyze` computed over whatever
   it was given. One non-finite value from one encoder made `movement_score = NaN` for the episode, and
   the verdict came out `unknown` — but only by accident, because every comparison against `NaN` is
   false. The *scores* were written to `episode_quality`, where a mean over a population containing one
   `NaN` returns `NaN` for the entire population. The monitoring baseline and every control limit
   derived from it inherited the corruption. One bad sensor silently moved the dataset-level view.
2. **`sqrt(sum(d*d))` raised `OverflowError` above ~1e154.** The data was valid; an intermediate product
   of the formula was not. The exception propagated out of the reader and failed the whole ingest job.
3. **One bad dimension condemned the episode.** The verdict was the *worst* dimension's band, on the
   reasoning that "a robot with one bad encoder is a robot with a bad encoder". Measured against a real
   bag, 17 clean joints and 1 noisy channel produced `jerky` — and nothing in the API told the operator
   which dimension did it. The visualizer's own relative banding is degenerate for the same reason
   (already corrected in 0018 §3); the worst-dim rule is the same failure wearing a different hat.
4. **`analyze` had no concept of time.** `movement_score` is an L2 norm *per frame transition*, so a
   robot frozen for five minutes and a robot moving continuously produce identical numbers. The values
   are the same; only the wall time between them differs. The catalog already computed
   `max_episode_timestamp_gap_seconds`, but in the monitoring subsystem, where it never reached the
   episode's own quality record and no episode-scoped UI could show it.

A fifth finding was not a defect but a hole in the contract: the synthetic ingest path accepted ragged
observation/action rows and failed three lines later with an `IndexError` inside the series builder,
surfacing as a retryable `INTERNAL_ERROR` and burning all three attempts on input that could never
succeed.

## Decision

1. **Non-finite values stop the analysis instead of propagating.** `analyze` counts them, returns zero
   scores with `verdict="unknown"`, and reports the count in `nonfinite`. Scoring zero is not a
   convenience: it is the only outcome that leaves every downstream mean true. A quality signal must
   never be the reason an ingest fails, and a poisoned aggregate is worse than a missing one.
2. **`math.hypot` for the L2 norm.** Specified not to overflow on intermediate results, so the
   arithmetic matches the data instead of failing a job over a detail with no bearing on its validity.
3. **The verdict is the *median* judged dimension, not the worst.** A majority of the arm has to be
   rough before the episode is called jerky. The worst band is still reported, as `worst_verdict` and
   `worst_dim`, so the change hides nothing and the UI can name the culprit. `judged_dims` is published
   so a caller can see how much evidence stood behind the verdict.
4. **Variance is computed over normalized deltas** (`norm_delta_std = sqrt(variance)`), removing a
   division by the dim range from the inside of the statistic. Discrete and gripper dims remain
   excluded from the verdict — a two-state signal has no notion of a smooth trajectory — but they are
   still *measured*, because a gripper that never moved is one of the things an operator needs to see.
5. **`analyze` takes optional timestamps and reports temporal integrity.** New fields `max_gap_seconds`,
   `gap_ratio` and `integrity` (`ok` / `gapped` / `unknown`). A transition is a gap when its interval
   exceeds 5× the **median** interval: median so one enormous drop cannot raise the bar above every
   legitimate interval and hide itself, relative so a healthy 1 kHz torque loop and a healthy 30 Hz arm
   both pass and neither needs a per-dataset threshold. A backwards clock is refused as `gapped` with no
   duration (every rate derived from it would be a lie); a non-finite clock yields `unknown`.
   Omitting timestamps is not an error — `integrity` is then `unknown`, never `ok`, because
   "the recording was continuous" is a claim about time that cannot be made without a clock.
6. **Ragged rows are rejected at the contract boundary** with a message naming the widths seen, so the
   failure is one clear validation error instead of an `IndexError` three layers down.
7. **All three readers supply the clock**: the LeRobot reader passes the `timestamp` column, the
   synthetic path passes `episode["timestamps"]`, and the MCAP reader carries a log-time series
   alongside its existing decimated value window, so the memory bound is unchanged.

   The MCAP clock is **trimmed by dropping its oldest half, not by stride-halving**, even though the
   value windows beside it still use `[::2]`. This split was not a design preference; the first
   implementation shared the stride and the end-to-end run immediately falsified it. A clock buffer
   halved with `[::2]` keeps sample 0 forever, so it degenerates into one ancient timestamp followed
   by a dense block of recent ones, and the gap detector reads the distance between them as a dropped
   recording: a clean 50 Hz, 600-second log was published as `integrity=gapped` with a 501-second
   maximum gap. Value windows are immune because their statistics ignore order and spacing. A
   contiguous recent window at true resolution cannot manufacture a gap.
8. **The reader also reports the exact whole-log maximum interval**, in constant memory, alongside the
   bounded window, and `analyze` takes it as `max_interval_seconds`. A bounded window cannot see a
   freeze that happened before it filled, and reporting `ok` there would be the same falsehood in the
   opposite direction. The override may raise `max_gap_seconds` and make `integrity` `gapped`; it may
   not touch `gap_ratio`, because a ratio over a retained window is not a ratio over the log.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Refuse, judge by majority, add time (chosen)** | Every aggregate stays true; a defect is visible and named; a frozen robot and a moving robot stop scoring identically; no new subsystem | Seven new columns on `episode_quality` and a small per-episode clock buffer in the MCAP reader |
| Sanitize non-finite values (interpolate, drop the frame) | Verdict still produced | Invents data. The gap is a fact about the recording and the operator needs to see it, not a hole to paper over |
| Keep the worst-dim verdict, expose `worst_dim` only | Minimal change | 1 noisy joint in 18 still labels a good episode `jerky`; the population verdict is wrong more often than it is right |
| Per-dataset gap thresholds in a validation profile (ADR 0016) | Matches each robot's real rate exactly | A profile that must be right before an operator can be told whether their recording dropped frames; the relative rule is right for the overwhelming majority and is never wrong in the direction that matters (a slow-but-continuous stream is not called broken) |
| Keep integrity in `monitoring` only, join it in at read time | No new columns on the hot ingest path | The signal is episode-intrinsic and belongs in the episode's record; a read-time join re-derives, per query, something already known at the one moment it was cheap to know |

## Consequences

- A bag with one encoder hiccup now shows `verdict="unknown"` and `nonfinite=1` rather than a plausible
  wrong number. That is the intended trade: absence over falsehood, stated where the operator sees it.
- `episode_quality` grows 7 columns; readers and the three quality queries publish them; the OpenAPI
  contract is regenerated (`EpisodeSummary`, `EpisodeQualityResponse`).
- The MCAP reader's memory bound is unchanged, but its clock and its value windows are now trimmed by
  *different* rules on the same trigger, so a future change that re-unifies them will reintroduce the
  false gap. Two tests in `tests/unit/readers/test_mcap.py` fail if it does, and both were confirmed
  to fail against the stride-halving version.
- The temporal signal describes the most recent `2 * QUALITY_WINDOW` accepted samples of a topic, which
  at 50 Hz is about 20 seconds. `max_gap_seconds` and `integrity` cover the whole log; `gap_ratio`
  covers only that window. This asymmetry is deliberate, and it is the one place in the product where a
  published number is scoped rather than universal.
- Verdict semantics changed, so episodes ingested before this ADR may carry a `worst`-based verdict in
  their stored `dims` JSON. The `verdict` column is recomputed only on re-ingest; content addressing
  means re-ingesting the same bytes is idempotent and is the supported way to refresh it.
- `integrity` is the only signal in the product that distinguishes "the robot was still" from "the
  recorder stopped". No per-frame statistic ever could, and this is what makes the temporal half worth
  its seven columns.
