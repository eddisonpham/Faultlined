# EXP-0019: What the behavioural fingerprint can see, and what it costs

- **Date (UTC):** 2026-10-07
- **Author/agent:** Buffy (implementer/benchmark engineer), curation improvement programme
- **Status:** done
- **Related:** [ADR 0032](../decisions/0032-behavioural-fingerprints.md),
  [the platform landscape](../research/2026-10-07-platform-landscape-and-curation.md),
  [the improvement ranking](../research/2026-10-07-curation-idea-improvements.md) idea C1,
  [benchmarking methodology](../benchmarking/methodology.md)
- **Harness:** `scripts/fingerprint_calibration.py` (writes nothing to the repository)

## Hypothesis / purpose

SemDeDup and FiftyOne Brain both answer "are these the same behaviour recorded twice" with an
embedding, which this engine cannot have (no numerical library in the runtime dependency set, no
frames in memory, no model in a decision path — ADR 0020). The claim under test is that a
**deterministic descriptor assembled from signals ingest already computed** — the motion trace,
per-dimension normalised motion character, and stall/gap fractions — separates a re-recording from
a different behaviour well enough to be worth a report.

Stated before running, so it can fail: if the desk cannot beat the corpus's own duplicate base
rate at any threshold, the measure carries no information and the idea is dead. It does not fail.
It fails to be *good* in one specific way, which is the more useful half of the result.

## Change under test

New: `data_engine/analysis/fingerprint.py` (pure), `PostgresCatalog.episode_fingerprint_inputs`,
`GET /api/v1/builds/{hash}/redundancy`, a redundancy section on `/ui/builds/{hash}`.
Baseline for the cost half: nothing — this did not exist before.

## Configuration

- Corpus: 60 distinct behaviours, each recorded 3 times (jitter + 2 % time warp), plus 2 *decoys*
  per behaviour — the same joints at the same speed, re-phased against each other. Decoys are the
  declared hard negative: they share every per-dimension statistic, so only the shape component
  can separate them.
- 300 episodes, 44 670 foreign (different-behaviour) pairs, 180 true re-recording pairs.
- Seed 20261007; components `{shape: 0.5, dynamics: 0.35, temporal: 0.15}`.
- Latency half: a throwaway Postgres on the isolated cluster (`bb_fingerprint`), seeded through
  the real writer (`register_episode` + `record_episode_quality` over `analyze()`), measured as
  the real read (`episode_fingerprint_inputs`) plus the real report.

## Provenance

Tree: this slice, uncommitted at measurement time; `scripts/fingerprint_calibration.py` at the
same revision. Hardware: the development laptop (the numbers are a shape, not a target — see
"Limitations"). No network. Raw output is the console table; nothing was written to
`benchmarks/results/`.

## Results

### Detection, against planted ground truth

| threshold | precision | recall | F1 | re-recordings caught | foreign pairs merged | distinct reported / actual |
|---|---|---|---|---|---|---|
| 0.01 | **1.000** | 0.217 | 0.356 | 39/180 | 0/44670 | 291/360 |
| 0.02 | **1.000** | 0.233 | 0.378 | 42/180 | 0/44670 | 288/360 |
| **0.04** | **0.826** | 0.394 | **0.534** | 71/180 | 15/44670 | 267/360 |
| 0.06 | 0.494 | 0.444 | 0.468 | 80/180 | 82/44670 | 214/360 |
| 0.10 | 0.084 | 0.456 | 0.141 | 82/180 | 898/44670 | 51/360 |
| 0.20 | 0.008 | 0.722 | 0.016 | 130/180 | 15570/44670 | 3/360 |

The duplicate base rate of this corpus is **0.004**, so precision 1.000 is not free — it is 250×
the base rate. The first shipped default (0.10) sat at 0.084: worse than declaring everything
distinct, and it merged 898 foreign pairs into duplicate groups. **The measurement invalidated
the default, and the shipped default is now 0.04** (precision 0.826, 15 foreign merges).

### Is the weighting doing anything? (component sweep, same corpus)

Six weightings — including shape-dominant, dynamics-dominant and temporal-near-zero variants —
were swept over thresholds. Best F1 at precision ≥ 0.9 ranged 0.55–0.59 with no ordering worth
distinguishing. **The weights are not the lever**, so the shipped `0.5 / 0.35 / 0.15` is kept, on
the record as measured rather than assumed.

### Cost, through the real path (p50 of 5, worst of 5)

| episodes | catalog read | report (score) | total p50 | total worst | distinct |
|---|---|---|---|---|---|
| 300 | 74 ms | 68 ms | **142 ms** | 205 ms | 225 |
| 500 | 92 ms | 126 ms | **218 ms** | 233 ms | 331 |
| 800 | 156 ms | 340 ms | 496 ms | 650 ms | 461 |
| 1 000 | 129 ms | 490 ms | 618 ms | 683 ms | 544 |
| 1 500 | 197 ms | 729 ms | 926 ms | 1 076 ms | 720 |
| 2 000 | 280 ms | 1 536 ms | **1 815 ms** | 1 871 ms | 865 |

Cost is superlinear in episodes because the scan is O(n × kept). Two changes came out of it:

1. **Sound early exits in the pairwise scan** — components are computed cheapest-first (two
   fractions, then the per-dimension overlap, then the 32-point shape) and a pair is abandoned as
   soon as one component alone exceeds the threshold, since a weighted mean is at least each
   component's own weighted share of it. Output is **bit-identical** (the accuracy table above is
   the same before and after) and the score at 300 episodes fell from 229 ms to 68 ms — **3.4×**.
   `tests/unit/test_fingerprint.py::test_the_pruning_shortcut_agrees_with_the_public_distance`
   pins the shortcut to `distance()` across a grid of pairs and thresholds, so it can never
   quietly *become* the specification.
2. **`MAX_EPISODES` 2 000 → 500**, chosen from this table rather than from taste: 500 is the
   largest measured size that keeps the page inside a quarter second, and 2 000 was a two-second
   page. A truncated report says so.

## Trade-offs

- **Precision is bought with recall.** At 0.04 the report catches 39 % of planted re-recordings.
  The bias is deliberate: a false merge asserts that two episodes are one behaviour and *hides*
  the distinct one, while a missed duplicate leaves redundancy the operator can still see. The
  report also proposes and never removes, so a review step absorbs what precision cannot.
- **The temporal component is nearly dead weight at this threshold.** Its weight is 0.15, so a
  pair differing only in stall/gap character has to differ by very little to collapse. Asserted as
  a test rather than left implicit.
- **The shape component's blind spot is coordination.** Aggregate per-frame motion is invariant
  to which joint moved when. Decoys are merged at a rate that rises with the threshold, and it is
  reported rather than buried: at 0.02, zero of 44 670 foreign pairs merge, and decoys are part of
  that population.

## Conclusion

**Decision: adopt, with the measured parameters.** The measure beats the base rate by two orders
of magnitude on planted data with zero foreign merges at 0.02 and 15 of 44 670 at 0.04, it needs
no model, no dependency, no artifact read and no network, and its whole read+score budget at 500
episodes is a quarter second.

**Two design defects were found by the measurement, not by reading the code**, and both are worth
keeping:

1. The decoy generator scaled joints. Every signal the descriptor reads is divided by the
   dimension's own range, so an affine rescale is *exactly* the transformation the measure is
   built to ignore — the decoys measured 0.0000 against their own originals, i.e. the "hard
   negatives" were duplicates and precision was being reported at 0.02 because the ground truth
   was wrong. A negative sample has to be constructed against what the measure is blind *to*,
   not against what it is blind *by design*.
2. The behaviour generator was not injective: indices 0 and 35 produced the same motion (same
   period from `index % 7`, same amplitude vector from `index % 5`), so 225 pairs of genuinely
   identical episodes were counted as false positives. The first run's precision of 0.036 was
   mostly the ground truth being wrong. **A calibration is only as good as its labels, and a
   synthetic corpus is a program that can be buggy like any other.**

## Limitations

- **Synthetic corpus.** These are planted re-recordings, not real teleoperation. There is no
  labelled duplicate set in the real fixtures, so **no claim is made about precision or recall on
  real robot data** — the honest statement is "measured on planted duplicates, unmeasured on real
  ones".
- The corpus's "different behaviour" pairs differ mainly by joint speed. Real differences
  (different objects, different scenes, different task success) are not represented at all.
- Laptop hardware; run-to-run spread on the cost half is visible (n=400 measured 280 ms p50 in
  one pass and n=450 measured 237 ms in the next). Treat the curve as a shape, not a target.
- The report's partition is greedy and order-dependent by construction: two episodes each similar
  to a third are not thereby similar to each other.

## Follow-ups

1. **Screening index (next iteration).** The superlinear scan is the binding constraint on
   `MAX_EPISODES`. A sound prefilter — bucket by a cheap invariant, or compare only against
   representatives whose rank is near — would let the bound rise. Deferred deliberately: the
   measurement above is the baseline it would have to beat.
2. **Real-corpus validation.** A build of two ingest passes over the same queue with different
   sensor noise is the cheapest real labelled duplicate set available; the A/B driver in this
   programme is the place to plant it.
3. Turn the now-measured properties into operator-facing text on the redundancy panel: state the
   threshold's precision/recall trade rather than only its value.
