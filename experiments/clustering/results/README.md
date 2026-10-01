# Findings — stage 2.5 clustering experiments

## EXP-2.5-01: no encoder separates the two gold classes at any single threshold

**Question.** Before choosing a clustering method: can sentence embeddings tell a
same-task pair from a different-task pair by distance? Every method downstream
assumes yes. If they cannot, no radius and no centroid rule can separate them, and
a factorial over methods would be ranking noise.

**Method.** 50 hand-built pairs over 78 distinct strings, each labelled per view
(`same_action`, `same_object`), 17 flagged ambiguous and excluded. Cosine similarity
per decided pair; sweep every candidate threshold; report balanced accuracy at the
best one, because the classes are unequal. Both views, four encoders, dev and
held-out pooled — this reports *where the classes sit*, it is not tuned against.

Each encoder passes a `verify` gate first (identical strings match to ~1.0,
unrelated score lower, deterministic across calls), so a mis-wired encoder cannot
produce a confident and meaningless number.

**Result.** Balanced accuracy at the best threshold:

| Encoder | kind | dim | action bacc | object bacc |
|---|---|---|---|---|
| `null` | hash of tokens | 64 | 0.750 | 0.640 |
| `m2v-8m` | static | 256 | 0.682 | 0.781 |
| `m2v-32m` | static | 512 | 0.683 | 0.808 |
| `minilm` | contextual | 384 | 0.644 | **0.883** |

**Finding 1. The classes interleave at every threshold, on both views, for all
four encoders.** There is no radius that separates same-task from different-task.

**Finding 2. Capacity is not the lever.** `m2v-8m` → `m2v-32m` moves the action
view by 0.001. Quadrupling the parameters buys nothing.

**Finding 3. Static encoders are dominated by lexical overlap.** A static encoder is
a weighted average of token vectors, so "pick up the red cube" and "push the red
cube" — different actions — score **0.836**.

**Finding 4. Contextual helps objects and hurts actions, by threshold.** Under
MiniLM the object view reaches 0.883 with the largest gap between classes anywhere
in the study. On the action view it is the *worst* encoder by threshold score.

**Finding 5. Error direction differs, and one of them is the dangerous one.**
At MiniLM's best threshold the object view produces **0 false merges and 7 false
splits**. A false merge silently mixes two tasks into one cluster and corrupts a
build; a false split shows up as two small clusters that a human merges in seconds.

**Caveats, stated rather than buried.**

- 37 decided action pairs is a small sample; balanced accuracy carries roughly
  ±0.12. Differences of that size are not significant, so the defensible claim is
  the negative one: *no measurable advantage from semantics at this sample size*.
  It is **not** "semantics hurt". This caveat is why EXP-2.5-02 exists.
- The claim in the stage 2.5 research record that this is "the rare problem where
  the right tool is obviously the right tool" was written before any measurement
  and does not survive one.

**Reproduce.**

```bash
just cluster separability --embedding null      # control, no semantics
just cluster separability --embedding m2v-8m     # static, 256d
just cluster separability --embedding m2v-32m    # static, 512d
just cluster separability --embedding minilm     # contextual, 384d
```

---

## A correction to EXP-2.5-01: its ceiling diagnostic was wrong

EXP-2.5-01 originally carried a column headed "supervised ceiling", computed as a
leave-one-out 1-nearest-neighbour over the gold labels. **That column was wrong and
every conclusion drawn from it has been retracted.** The number now reported is
called a *probe* and is a different measurement; the old metric has been deleted.

The bug: the metric stamped each pair's label onto **both of the pair's strings**
and then asked whether a string's nearest neighbour shared that label. But the label
belongs to the pair, not the string. "pick up the red cube" is the same action as
"grab the red cube" and a *different* action from "push the red cube", so it carries
both labels at once:

| set | view | strings carrying both labels |
|---|---|---|
| hand | action | 3 of 57 (5%) |
| hand | object | 8 of 73 (11%) |
| constructed | action | 135 of 494 (27%) |
| constructed | object | 113 of 494 (23%) |

A per-string classifier asked a question with no consistent answer answers at
chance. On the constructed set it reported 0.478–0.488 on the action view — at or
below the 0.5 coin flip.

This matters beyond the deleted column: EXP-2.5-01's Finding 4 rested on MiniLM
having "the highest supervised ceiling (0.770)", and used it to argue the action
information was present but unreachable by a threshold. With 5% of hand action
strings doubly labelled, that 0.770 was inflated by the same defect. **The claim
that the action information is present was not supported by this measurement and is
retracted** — though EXP-2.5-02 re-establishes it properly, by a different route.

## EXP-2.5-02: the two views need opposite treatments, and the probe establishes it

**Question.** Is the failure in EXP-2.5-01 that the information is absent from the
embeddings, or that a single threshold cannot reach it? The answer decides whether
a learned projection is worth building.

**Method.** Two independent measurements per view, on both gold sets.

*Threshold.* As EXP-2.5-01: balanced accuracy at the best cosine threshold.

*Probe.* A closed-form ridge regression on pair features — the difference vector,
plus its length, plus an intercept — fitted on dev pairs and scored on held-out
pairs the fit never saw. Regularisation strength is chosen by **leave-one-component-out**
cross-validation inside dev.

The probe is a *pair* classifier, which is the correction above: the label is a
property of the pair. Its ridge penalty is a solved `n×n` system rather than a
gradient fit, so it is exact, deterministic, and adds no dependency beyond numpy.

**The constructed gold set.** 385 pairs over 494 strings whose labels are exact by
construction rather than by judgement: 17 verb classes × object names, four balanced
cells. 196 action positives against 189 negatives, where the hand set has 37 decided
action pairs carrying ±0.12. This is what makes the numbers below resolvable.

Two labelling rules, deliberately not symmetric. Actions compare by **verb class**,
so "grab" and "pick up" are one action. Objects compare by **exact name**, so
"table" and "counter" are different objects — matching how the hand set labels the
same comparison. Being consistent with the hand set matters more than being
generous, because two sets that label the same pair differently cannot be compared.

`per_cell` is capped at 100 by measurement, not taste. At 150 the stride goes to 1,
strings get reused across cells, class-identity grouping chains the vocabulary into
one component of 488 pairs, and held-out collapses to 90. That failure is silent —
it still passes a leakage check — so `gold_grid.split` raises on it.

**Result — constructed set, 385 pairs, 195 dev / 190 held-out, split clean:**

| Encoder | action threshold | action probe | object threshold | object probe |
|---|---|---|---|---|
| `null` | 0.577 | **0.816** | 0.741 | 0.786 |
| `m2v-8m` | 0.717 | 0.783 | 0.870 | 0.859 |
| `m2v-32m` | 0.728 | 0.815 | 0.877 | 0.869 |
| `minilm` | 0.679 | 0.736 | 0.956 | **0.959** |

**Result — hand set, 50 pairs** (same columns, far noisier):

| Encoder | action threshold | action probe | object threshold | object probe |
|---|---|---|---|---|
| `null` | 0.750 | **0.792** | 0.577 | 0.514 |
| `m2v-8m` | 0.682 | 0.625 | 0.781 | 0.752 |
| `m2v-32m` | 0.683 | 0.625 | 0.808 | 0.643 |
| `minilm` | 0.644 | 0.583 | 0.883 | 0.719 |

**Finding 1. The action information is present, and a threshold cannot reach it.**
On the constructed set the probe beats the best threshold on the action view for
every encoder, by +0.06 (`null`) to +0.24 (`m2v-8m`). The gap is large, consistent
in sign across four encoders and both gold sets, and it is the measurement EXP-2.5-01
was reaching for and got wrong. This is the one finding that justifies building the
learned projection.

**Finding 2. The object view needs no projection at all.** MiniLM already reaches
0.956 by threshold with only 10 false merges across 385 pairs, and the probe lifts it
to 0.959 — a gain of 0.003, i.e. nothing. Contextual embeddings already do this
job; there is no headroom for a learned method to claim.

**Finding 3. The two views therefore need opposite treatments,** and this is the
finding that decides the design. The object view is solved by an off-the-shelf
encoder plus a radius. The action view is not solvable that way and needs a learned
projection. Shipping one metric for both would be wrong in one direction or the other.

**Finding 4. Error direction, re-measured with power.** At MiniLM's best object
threshold there are 10 false merges and 7 false splits; on the action view, 105
false merges against 17 false splits. The object view is the one that can be shipped
without a human in the loop.

**Caveats, stated rather than buried.**

- **The constructed set under-discriminates encoders on the action view.** The
  `null` encoder — a 64-dimensional hash of tokens, no semantics at all — probes at
  0.816, the *highest* action score of the four. Its action labels are generated from
  a small template vocabulary, so the tokens nearly determine the label. Read the
  action column as an upper bound on what is expressible, not as an encoder ranking.
  The object column, where the labels depend on which noun was substituted into a
  fixed frame, discriminates properly and is the one to choose an encoder from.
- The hand set cannot choose an encoder on the action view at all. Its 37 decided
  pairs carry ±0.12 and the four action probe scores span 0.583–0.792, which is
  inside the noise. The two sets agree on direction (probe > threshold on actions,
  both well above chance) and disagree on ranking, and the disagreement is explained
  by the first caveat.
- Pair-level independence is weaker than pair count suggests. The constructed set's
  385 pairs sit in 93 leakage components, so the effective sample size is nearer 93
  than 385. Leave-one-**pair**-out cross-validation inside dev scores a perfect 1.000
  on every encoder because a held-out paraphrase of a training pair is still the same
  task; the fold therefore uses the component, which is the same unit of independence
  the split uses.
- The probe is an upper bound on a *linear* projection. It says nothing about
  whether an online, frozen-centroid clustering can exploit it in deployment, which
  is a stricter test and is what the factorial exists to answer.

**Three defects found in the probe while building it**, each of which silently
produced plausible nonsense rather than an error:

1. The original 1-NN ceiling scored pairs as strings (above) — chance-level.
2. Regularisation strength was chosen on the **training** score, which improves
   monotonically with model flexibility. Every encoder chose the weakest
   regularisation and reported the overfitted fit's held-out number. It is now
   chosen by component-wise cross-validation inside dev.
3. `1 - h_ii` for the leave-one-out residual was taken from the solved dual weights
   instead of the inverse's diagonal. That divides by the wrong quantity and scores
   **every** model at exactly 0.500. Caught by checking the closed form against
   explicit per-fold refitting, now a test.

A fourth was found by construction rather than by inspection: the probe originally
normalised each difference vector, discarding its length — which for unit-norm
embeddings is a monotone function of cosine distance, i.e. the primary evidence. It
then still failed on a fixture whose only signal was that two near-identical vectors
sit close together, because magnitude is not a linear function of the difference.
The length is now an explicit feature column and an intercept is included, which
together guarantee the probe is at least as informative as the threshold it is meant
to beat.

**Reproduce.**

```bash
just cluster gold --set constructed               # the set and its leakage report
just cluster separability --set constructed --embedding minilm
just cluster separability --set hand --embedding minilm
```

## Not yet run

- `centroids` — built and passing, but last run with a radius chosen arbitrarily
  before the premise test existed. Held until a representation separates the classes,
  which EXP-2.5-02 now identifies: MiniLM for objects, a learned projection for
  actions.
- the learned action/object projection itself, and the method × view × encoder
  factorial that depends on it.
- the manual boundary audit, which needs a finalist.