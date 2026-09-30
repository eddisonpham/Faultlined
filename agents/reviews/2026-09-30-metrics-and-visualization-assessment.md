# Review: evaluation metrics, end-to-end usefulness, and the missing visual layer — 2026-09-30

- **Reviewer role:** implementer agent
- **Commits reviewed:** `f5a9015` (metrics hardening), `dee64dc` (decimation fix)
- **Stage being accepted:** post-MVP, stage 3 (observability surface)

This is the third pass over the same system. The first built it, the second ran it end to end and
found five defects, and this one hardened the metrics, ran it again, and found a sixth — one the
second run's own new code introduced. That sequence is the argument for the rest of this document:
the product's weak point is not missing features, it is that **numbers reach the catalog without
anything checking whether they mean what they say.**

## 1. What the metrics audit found

An adversarial probe (`var/run/probe_metrics.py`, seven sections) fed the quality analyzer the
inputs real hardware produces. Five findings, now fixed and pinned (ADR 0023, 26 edge-case tests):

| # | Finding | Consequence before the fix |
|---|---|---|
| 1 | A single `NaN` from one encoder | `movement_score = NaN` written to the catalog; every population mean, the monitoring baseline and every control limit derived from it returned `NaN` for the **whole dataset** |
| 2 | `sqrt(sum(d*d))` above ~1e154 | `OverflowError` failed an entire ingest over a detail of the formula, not the data |
| 3 | The verdict was the *worst* dimension's band | 17 clean joints and 1 noisy channel condemned a good episode, and nothing named the culprit |
| 4 | `analyze` had no concept of time | A robot frozen for five minutes and one moving continuously scored identically |
| 5 | Ragged synthetic rows accepted at the boundary | `IndexError` three layers down, surfaced as retryable `INTERNAL_ERROR`, burning three attempts on input that could never succeed |

Finding 4 was a design gap rather than a bug, and it is the most valuable thing in this pass. It
produced `integrity`, `max_gap_seconds` and `gap_ratio` — the only signals in the product that can
distinguish **"the robot was still"** from **"the recorder stopped"**.

## 2. The end-to-end run found a sixth defect, in the fix for the fifth

Re-running the 86-assertion driver from an empty catalog, across all three readers, the new
temporal signal immediately reported the 20-minute MCAP log as `integrity=gapped` with a **501-second
maximum gap**. The log is a clean 50 Hz stream. Measured directly from the file:

```
/joint_states messages: 30000
largest gap: 0.02 s      negative intervals: 0
```

The hole was manufactured by the reader. The clock buffer was halved with `[::2]` in lockstep with
the value windows. That is correct for values — their statistics ignore order and spacing — and
wrong for a clock: sample 0 survives every halving, so the buffer degenerated into one ancient
timestamp followed by a dense block of recent ones, and the gap detector read the distance between
them as a dropped recording. Reproduced in isolation in nine lines, then fixed by trimming the
clock's oldest half instead, plus an exact whole-log maximum tracked in constant memory.

Both regression tests were confirmed to **fail** against the stride-halving version before the fix.

This is worth stating plainly: the temporal signal shipped broken in the same commit that
introduced it, and only a real run against a real file caught it. No unit test did, because every
unit test used a series shorter than the window. `tests/unit/readers/test_mcap.py` now builds a bag
of `8 * QUALITY_WINDOW` messages precisely so that the buffer is trimmed at least three times.

**86/86 assertions pass from an empty catalog. 703 unit tests, 92.00% coverage, mypy strict clean.**

## 3. How useful is it, honestly?

Judged as a tool I would actually keep open while a robot is recording:

**Genuinely working, and hard to get elsewhere.**
- Content-addressed builds: rebuilding the same selection reproduces the same hash; a different
  policy is a different hash. The lineage answer "where did this training set come from" is exact.
- Quarantine that means something: the synthetic episode was quarantined for `TOO_FEW_FRAMES` and is
  in no build, and the reverse-lineage endpoint confirms it.
- The quality signals are cheap, episode-intrinsic, and computed while the bytes are in memory. No
  re-read, no second pass, no framework.
- Three input formats, one output contract, from a 20 MB real bag and real LeRobot fixtures.

**Working, but the operator cannot see it.** This is the whole problem, and it is a presentation
problem rather than a capability problem.

### 3.1 The numbers are not comparable, and the chart proves it

`/ui/insights` draws `_scatter_svg(speed, "movement_score")` over the population. From the real run:

```
100015978.52725415, 3.5397, 2.3085, 0.16099, 0.02236
```

One episode is a driving dataset in millimetres; the others are arm joints in radians. On a linear
axis the four small episodes are indistinguishable points on the floor. The chart is not wrong; it
is drawing a quantity — "speed" — that has no shared unit. **`movement_score` is an L2 norm in raw
units, so cross-dataset comparison is meaningless, and the product's headline curation view is built
on exactly that comparison.**

The fix is nearly free: `analyze` already computes `mean_abs_delta_norm` (mean |Δ| divided by the
dimension's own range) for every dimension. A population-level normalized score is the same
arithmetic one level up, and unlike the raw score it is scale-free and therefore comparable.

### 3.2 The runtime metrics exist and are thrown away

ADR 0017 aggregates eleven series — `api_request_duration_seconds`, `jobs_queue_time_seconds`,
`jobs_run_time_seconds`, `pipeline_stage_duration_seconds`, `episodes_ingested_total`,
`jobs_queue_depth` and more — bucketed by minute. `/ui/metrics` renders each as a bare
`<polyline>`: 44 pixels tall, no axes, no units, no scale labels, no legend, and no way to ask
"which ten jobs made that 0.44 s?" The data to build a real dashboard has been collected since
scaffolding and is displayed as five decorative squiggles.

### 3.3 The temporal signal is a scalar where it should be a picture

`integrity` is one word per episode. The operator's actual question — *where* did the recorder
stall, and for how long — is answered by a number they cannot locate. The reader already keeps a
bounded, contiguous, true-resolution window of the log; keeping the per-frame motion magnitude
alongside it would cost the same bounded memory and would make the gaps **visible as literal holes
in a line**, which is how every monitoring tool an operator has ever used presents a dropout.

### 3.4 Nothing aggregates recording reliability

Five episodes, five different recorders, five chances for the hardware to fail, and the product
stores each verdict separately. A dataset assembled from gappy recordings is a dataset with holes
in it, and **nothing in the pipeline notices, because nothing counts.** `monitoring.features` has
`max_episode_timestamp_gap_seconds`, and it never reaches the episode's own record — the two halves
of the product could not previously see each other.

## 4. What is missing, ranked

| # | Gap | Cost | Why it matters |
|---|---|---|---|
| 1 | No scale-free motion score | small | The curation view's primary chart is currently meaningless across datasets |
| 2 | No real time-series dashboard | medium | Eleven collected series are displayed as five unlabelled squiggles |
| 3 | No schema or data-flow visualization | medium | 14 tables, 20+ endpoints, and no page that shows how data moves through them |
| 4 | No lineage graph | small | The DAG exists as an API and is rendered as a table |
| 5 | No recording-reliability aggregate | small | Per-episode integrity, never rolled up |
| 6 | No episode motion trace | medium | Turns `integrity` from a word into something an operator can see |

## 5. Recommendation: the next stage

Three pages and two small analysis additions, in this order.

1. **Fix the metric before charting it.** Add a scale-free population score next to the raw one and
   plot that. Charting `movement_score` first would ship a broken chart with a confident look.
2. **`/ui/observability`** — a real dashboard over the series that already exist: axes with units,
   bucket labels, quantile bands, and drilldown from a point to the jobs or episodes in that bucket.
3. **`/ui/schema`** — the live catalog schema read by introspection, the foreign-key graph drawn
   from the real constraints, and the ingest path overlaid, so the page shows where data actually
   flows instead of a hand-maintained diagram that can silently disagree with the code.
4. **Lineage DAG** on the build and episode detail pages, drawn from the existing endpoints.
5. **Motion trace** per episode, so a gapped recording looks gapped.

Items 1 and 5 are analysis changes and need an ADR; items 2–4 are presentation and extend the
inline-SVG idiom already established in ADR 0021. All of it stays server-rendered with no bundler,
no CDN and no third-party JavaScript, per ADR 0014.

## 6. Checklist

- [x] Architecture docs match the code — ADR 0023 written before the change it records
- [x] Every significant decision has an ADR — 0023 amends 0018 rather than editing it
- [x] Interfaces match `architecture/api.md` — OpenAPI contract regenerated and checked in CI
- [x] No dead code, no speculative abstraction — `max_interval_seconds` has two callers and three tests
- [x] No new dependency — the entire visual layer is stdlib string building
- [x] Format, lint, type-check, tests, hygiene green
- [x] Coverage gate enforced, not lowered (92.00%, up from 91.97%)
- [x] Benchmarks unchanged — the value windows still use `[::2]`, so EXP-0004 still describes the
      ingest path; the clock is a bounded list append, not a second pass over the payload
- [x] No secrets written, logged or committed
- [x] Docs in `agents/` current; CLAUDE.md pointers valid

## 7. Findings

| # | Severity | Finding | Action | Status |
|---|---|---|---|---|
| 1 | blocker | NaN in one sensor poisoned every dataset-level aggregate | refuse and count (ADR 0023 §1) | fixed |
| 2 | blocker | `OverflowError` failed whole ingests on valid data | `math.hypot` (§2) | fixed |
| 3 | major | One noisy joint condemned a clean episode; culprit unnamed | median verdict + `worst_dim` (§3) | fixed |
| 4 | major | No temporal signal: a frozen robot scored like a moving one | `integrity`, `max_gap_seconds`, `gap_ratio` (§5) | fixed |
| 5 | major | Ragged synthetic rows failed as retryable `INTERNAL_ERROR` | rejected at the contract boundary (§6) | fixed |
| 6 | blocker | Clock decimation invented a 501 s gap in a clean log | trim oldest half + exact whole-log max (§7, §8) | fixed |
| 7 | major | `movement_score` is not comparable across datasets; the curation chart is meaningless | plot the dimensionless `jerk_score` | **fixed** |
| 8 | major | Eleven metric series rendered as unlabelled squiggles | plots with axes, units, peak and drilldown | **fixed** |
| 9 | minor | No schema or data-flow view of 14 tables | `/ui/schema`, introspected live | **fixed** |
| 10 | minor | Lineage existed only as JSON | `/ui/builds` + a lineage DAG | **fixed** |
| 11 | minor | Per-episode integrity was never rolled up | reliability aggregate on Insights | **fixed** |
| 12 | major | The clock's stride-halving manufactured a 501 s gap in a clean log | trim the oldest half + exact whole-log max | **fixed** |
| 13 | minor | `referenced_by` attached to the wrong side of each foreign key | attach to the referenced table | **fixed** |

## 9. After delivery: the browser and the human eye

A source-reading pass misses whole classes of defect, so this round verified the new
pages in a real browser (Playwright: geometry, clipping, contrast, console) and then a
human looked at the screen. The human found more.

| # | severity | finding | remedy | status |
|---|---|---|---|---|
| 14 | major | `/ui/incidents` polled its own URL and ignored `X-Fragment`, so after the first tick the poller nested the entire page - second nav bar included - inside the panel (human-found) | the route honours `X-Fragment`; the orphaned `/ui/incidents/fragment` route is deleted; `tests/contract/test_ui_polling.py` asserts every polled page's own poll target answers bare | **fixed** |
| 15 | minor | the vendored phosphor `text-shadow` halo on readouts and headings reads as cheap decoration (human-found) | overridden off in `faultlined.css` rule 2, scoped to `text-shadow` alone; the neon colours are untouched and a test pins that scope | **fixed** |

## 8. What the next stage actually delivered

Findings 7–13 are closed. The order was not arbitrary: **the metric was fixed before it was
charted**, because charting `movement_score` first would have shipped a broken chart with a
confident appearance.

The scale-free score turned out not to need a new column. `analyze` was already computing
`mean_abs_delta_norm` — mean absolute delta divided by the dimension's own range — for every
dimension, and `jerk_score` is its mean over the active ones. It was already dimensionless and
already comparable; it was simply not the thing the chart was plotting. The reference run's five
episodes, before and after:

```
movement_score : 100015978.5,  3.54,  2.31,  0.161,  0.022    (4.5 billion-x spread)
jerk_score     :          0.0185,  0.0116,  0.0079,  0.0042     (4.4x spread)
```

**777 tests, 91.96% coverage, mypy strict clean over 62 modules, and 109/109 end-to-end
assertions from an empty catalog** across all three readers, both build policies, and the new
visual pages. Two defects in this stage (12 and 13) were found by the work of this stage itself: one
by re-running the end-to-end driver, one by writing the test for a column nobody had looked at
closely. Both are now pinned.
