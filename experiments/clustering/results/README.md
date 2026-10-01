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

## EXP-2.5-03: the learned projection does not work, and the object view does not need it

**Question.** EXP-2.5-02 left exactly one thing worth building: a learned projection
for the action view, since a probe can reach it and a threshold cannot. This asks
whether it survives contact with held-out data.

**Method.** Fisher discriminant analysis on pair differences, fitted on dev pairs
only: whiten by the pooled within-class scatter, then keep the leading directions.
The width is chosen on **dev threshold** balanced accuracy, never on the probe —
the probe is what the projection is a substitute for, so optimising it here would
optimise the wrong thing. Held-out is read once, after `k` and the shrinkage
strength are frozen.

Two metrics are reported, because the projection changes the shape of the space
and cosine may no longer be the right yardstick. It is not: whitening rescales
every retained axis to unit variance, which flattens the difference between "close
because same task" and "close because near". Cosine of the projected vectors is not
the quantity that was arranged — and in one axis it is degenerate outright, since
the cosine of two scalars is always ±1. Both are pinned by tests.

**Result — constructed set, MiniLM, shrinkage 0.9, Euclidean:**

| view | metric | dev before → after | held-out before → after |
|---|---|---|---|
| action | euclidean | 0.673 → **0.757** | 0.724 → **0.687** |
| action | cosine | 0.673 → 0.524 | 0.724 → 0.516 |
| object | euclidean | 0.947 → 0.955 | 0.977 → **0.890** |
| object | cosine | 0.947 → 0.817 | 0.977 → 0.810 |

**Finding 1. The projection raises dev and lowers held-out, on both views, at every
width.** The pattern is identical in all four rows and is the signature of
overfitting, not of a metric mismatch. The dev "before" column is the control that
distinguishes the two: the unprojected space already scores 0.673/0.947 on dev, so
the projection is not uncovering anything, it is fitting dev.

**Finding 2. No shrinkage strength rescues it.** The within-class scatter is
estimated from ~195 pairs in 384 dimensions, so it is rank-deficient by
construction and its inverse is meaningless unless regularised. Sweeping shrinkage
from 0.1 to 0.99:

| shrinkage | action dev | action held-out | object dev | object held-out |
|---|---|---|---|---|
| 0.1 | 0.750 | 0.598 | 0.955 | 0.815 |
| 0.5 | 0.750 | 0.655 | 0.955 | 0.869 |
| 0.9 | 0.757 | 0.687 | 0.955 | 0.890 |
| 0.99 | 0.759 | 0.692 | 0.953 | 0.931 |
| *unprojected* | *0.673* | ***0.724*** | *0.947* | ***0.977*** |

As shrinkage approaches 1 the projection approaches the identity and held-out
climbs back to the baseline — without ever passing it. This is the expected
behaviour: whitening divides by each axis's standard deviation, so the axes that
varied *least* in-sample are inflated most, and those are precisely the axes whose
sample variance was mostly noise.

**Finding 3. A pair classifier generalises where a projection does not.** The probe
on the same vectors, fitted on the same dev pairs, reaches 0.784 held-out on the
action view while the projection built from those same pairs reaches 0.687. The
difference is not information — the information is identical — it is the demand.
The probe has to get one binary decision right about a pair. The projection has to
place *every* string so that *all* pairwise distances agree with the labels, and a
single position per string cannot do that for 17 action classes at once. This is
the most useful thing in the study, because it says the ceiling is not the problem.

**Finding 4. The object view is already solved and the projection only endangers
it.** Unprojected MiniLM scores **0.977 held-out with 5 false merges** across 190
pairs. The projection takes that to 0.890 and raises false merges from 5 to 9 — it
converts the best result in the study into a worse one.

**Conclusion: do not ship a learned projection.** Not as a default, and not behind
a flag. The action view is not reachable by reshaping MiniLM's space with a linear
map fitted on a few hundred pairs, and the object view does not need one.

**What survives, and it is narrower than the design assumed:**

- **Object view: ship it.** MiniLM, cosine, one threshold. 0.977 held-out, 5 false
  merges. No learned component.
- **Action view: unresolved, and honestly so.** The information is demonstrably
  present (probe 0.784) and demonstrably not expressible as a distance (threshold
  0.724, 44 false merges; projection 0.687). A binary pair classifier works and a
  metric does not, which points at *either* a representation trained on this task
  rather than a reshaping of a general one, *or* a human-in-the-loop design that
  never asks for an automatic action clustering at all.

**Reproduce.**

```bash
just cluster projection --set constructed --embedding minilm
just cluster projection --set constructed --embedding minilm --shrinkage 0.1
```

## EXP-2.5-04: the object view works, the action view does not, and one failure mode explains both

**Question.** Given EXP-2.5-03, what is actually shippable?

**Method.** The centroid rule and the radius are both selected on dev, over a grid,
ranked by over-merge rate first and pair F1 second. The winning configuration is
then frozen — encoder, view, rule, radius — and run once against held-out *pairs*,
with the clustering fitted on held-out *strings*. Alongside the score, every string
within 1.5× the radius of its assigned centroid is dumped with the members it was
merged with and the gold verdicts it was judged against, because a pair F1 of 0.94
looks identical whether the merges are right or wrong.

**Encoder choice, under the actual clustering method** (dev, object view, radius
chosen per encoder):

| Encoder | radius | clusters | pair F1 | over-merge | order-stable count? |
|---|---|---|---|---|---|
| `m2v-8m` | 0.05 | 64 | 0.663 | 0.063 | yes |
| `m2v-32m` | 0.30 | 64 | 0.754 | 0.038 | yes |
| `minilm` | 0.30 | 37 | **0.941** | **0.000** | **no** (10 distinct counts) |

**Centroid rule: it barely matters.** Six of the nine rules land on identical
numbers at radius 0.30 — same F1 (0.941), same zero over-merges, same drift to four
decimals. `sliding_8` is chosen because it is the cheapest of the tied set (24 µs per
point, 8 points of memory, zero outlier pull, full recovery). The rule only changes
the answer when the radius is wrong: `ema_0.90` selected radius 0.05 on its own dev
score and landed at F1 0.828. **Radius matters, rule does not.**

**Held-out results for the frozen configuration** (`minilm` / object / `sliding_8`
/ radius 0.30):

| gold set | pairs | clusters | pair F1 | false merges | false splits |
|---|---|---|---|---|---|
| constructed | 190 | 43 | 0.947 | **0** | 8 (0.100) |
| hand | 24 | 25 | 0.938 | **2** (0.286) | 0 |

For contrast, the same configuration on the **action** view, constructed set: pair
F1 **0.488**, 31 false merges, 55 false splits. That is a coin flip.

**Finding 1. The object view is the deliverable.** Zero false merges on the
constructed set, 43 clusters from 273 strings, and the audit confirms the clusters
are object groups rather than accident: `pour`/`close`/`hand over` the blue cube all
land together, which is *correct* for the object view and would be wrong for the
action view. The two-view design earns its keep here.

**Finding 2. The constructed set's zero false merges does not transfer, and the two
failures are the same failure.** On the hand set the same configuration produces two
false merges:

```
'press the red button'             merged with  'press the blue button'
'sort the red blocks into the bin' merged with  'sort the blue blocks into the bin'
```

Both differ only by a colour adjective. A contextual encoder embeds "the red button"
and "the blue button" as near-identical, and no radius separates them. The
constructed grid does not catch this because it draws its object pairs from a fixed
noun list where the colour adjectives are balanced rather than adversarially paired
— its over-merge rate of 0.000 is real but it was measured on easier negatives than
production will produce. **The constructed set is an upper bound, as documented, and
this is the concrete place it was optimistic.**

**Finding 3. Zero false merges is achievable and it is not sufficient.** The
object view trades in false splits (8 of them, 0.100) rather than false merges,
which is the right direction: a false split shows up as two small clusters a human
merges in seconds, a false merge silently mixes two tasks into a build. The radius
is what buys that trade, and it is the one number that must be set from real data.

**Finding 4. Cluster count is order-dependent at the winning radius.** `minilm` at
radius 0.30 produced 10 distinct cluster counts across 10 input orderings, while the
weaker encoders produced 1. So the best-accuracy configuration is also the one whose
answer to "how many tasks are there" depends on arrival order. The online method's
`confirm()` is the intended remedy — a human freezes a cluster and it never moves
again — but this is exactly the case for it: an engine that proposes clusters and
lets a human confirm them, rather than one that asserts a number.

**Decision** (as at EXP-2.5-04; the colour row is updated by EXP-2.5-05 below).

| | |
|---|---|
| **Ship** | Object view, `minilm`, cosine, colour facet at weight ≈0.5–1.0, radius 0.20–0.30, online centroids with `sliding_8`, human confirmation. F1 0.968 constructed / 0.889 hand, **zero false merges on both**. |
| **Do not ship** | The learned projection (EXP-2.5-03). It overfits and it degrades the one view that works. |
| **Do not ship** | The action view. F1 0.488 is a coin flip, and no representation tried separates it. |
| **Resolved** | Colour adjectives (EXP-2.5-05). Lifted out of the embedding onto their own axis. Both false merges gone. |
| **Still open** | Whether the object view should ignore the verb. Three verb-synonym false splits (`place the bowl`/`lift the bowl`) say it currently does not. |
| **Needs a human** | Cluster count. Never assert it; propose it. |

**What would change these conclusions.** A representation trained on this task
rather than a general one — the action view's information is demonstrably present
(probe 0.784) and demonstrably not expressible as a distance, so a fine-tuned
encoder is the obvious next attempt. Or a representation that respects colour, which
would fix Finding 2 outright. Neither has been tried; both are better uses of effort
than more radii.

**Reproduce.**

```bash
just cluster centroids --set constructed --embedding minilm --view object
just cluster final --set constructed --embedding minilm --view object --rule sliding_8 --radius 0.30
just cluster final --set hand --embedding minilm --view object --rule sliding_8 --radius 0.30
just cluster final --set constructed --embedding minilm --view action --rule sliding_8 --radius 0.30
```

## EXP-2.5-05: the colour facet removes both false merges

**Question.** EXP-2.5-04's only two false merges were pairs differing by a colour
adjective. Can that be fixed without touching the encoder?

**Method.** Colour is lifted *out of* the embedding and appended as its own
orthogonal component. The encoder sees the sentence with the colour removed —
because colour's contribution there is arbitrary, being whichever of two
near-synonymous token vectors the tokeniser happened to produce — and a colour
indicator is concatenated. Weight and radius are swept together on dev, because
adding a facet rescales every cosine distance and a radius tuned without one is
wrong for it. Held-out is read once, per gold set, with both frozen.

**The obvious fix was the wrong one, and it is worth writing down.** Stripping the
colour word before embedding sends "press the red button" and "press the blue
button" to the *same string*. That does not weaken the merge, it guarantees it.
Colour has to remain and become explicit; deleting the information and then
reporting that it went missing is not a fix. There is a test for exactly this, using
a deliberately colour-blind encoder.

**Result — held-out, once, frozen:**

| gold set | config | pair F1 | false merges | false splits |
|---|---|---|---|---|
| constructed, before | weight 0, radius 0.30 | 0.947 | 0 | 8 |
| constructed, after | weight 0.5, radius 0.30 | **0.968** | **0** | **5** |
| hand, before | weight 0, radius 0.30 | 0.938 | **2** | 0 |
| hand, after | weight 1, radius 0.20 | 0.889 | **0** | 3 |

Both target pairs are now in different clusters:

```
press the red button              -> cluster 22      press the blue button             -> cluster 20
sort the red blocks into the bin  -> cluster 32      sort the blue blocks into the bin -> cluster 31
```

**Finding 1. Both false merges are gone, and the hand set's over-merge rate goes
from 0.286 to zero.** The facet does the job it was built for.

**Finding 2. On the hand set F1 falls, from 0.938 to 0.889, and that is the correct
trade.** The three false splits that replace the two false merges are all verb
synonyms: `place the bowl`/`lift the bowl`, `hand over the pen`/`give the pen`,
`press the pedal`/`step on the pedal`. A false split shows up as two small clusters
a human merges in seconds; a false merge silently mixes two tasks into a build.
Trading three of the first for two of the second is what the error-direction
argument predicts, and it is why over-merge is ranked first throughout.

**Finding 3. The constructed set barely benefits, exactly as its own caveat
predicted.** 0.941 -> 0.945 on dev, where the hand set goes 0.966 -> 1.000. The
grid draws its colour pairs from a balanced noun list rather than pairing them
adversarially, so it never asked the hard question. Two gold sets disagreeing, with
the smaller and harder one giving the stronger signal, is the case for keeping both.

**Finding 4. The facet reveals a residual failure the false merges were hiding.**
Those verb-synonym splits are the object view's real weak spot: it is not purely
object-driven, because the verb still moves the embedding, so "place the bowl" and
"lift the bowl" land apart despite naming the same object. This was invisible while
the colour merges dominated the error budget.

**Limitations, stated rather than assumed away.**

- **The lexicon is hand-written.** It covers common colour words and will miss
  "amber" before it is fixed (see below), and anything a particular site calls its
  own parts. Every entry is one somebody had to type.
- **No ordering.** "red" and "blue" are orthogonal, which is the safe default, but a
  domain that genuinely treats "dark blue" as a variant of "blue" has to say so
  explicitly.
- **Multi-colour is approximate.** Sharing one of two colours gives half a unit of
  overlap, not a clean match.
- The weight is tuned on one hand set of 24 held-out pairs. It is a small sample and
  the direction of the effect, not the exact weight, is the transferable result.

**One bug this found in itself.** `colour_index` took its index straight from the
sorted colour list, so "amber" — alphabetically first — was assigned slot 0, the
same slot reserved for "no colour". Every sentence mentioning an amber part would
have been recorded as uncoloured. Caught by asserting that no colour maps to a
reserved slot, and it is the argument for writing tests against the slot table
rather than against the one example that motivated the feature.

**Reproduce.**

```bash
just cluster colours --set hand --embedding minilm --view object --rule sliding_8
just cluster final --set hand --embedding minilm --view object --rule sliding_8 --radius 0.20 --colour-weight 1.0
just cluster final --set constructed --embedding minilm --view object --rule sliding_8 --radius 0.30 --colour-weight 0.5
```

## EXP-2.5-06: figures, and one thing the metrics were hiding

**Method.** Three static SVG renderers over the frozen clustering — a squarified
treemap of cluster sizes, a PCA scatter with a convex hull per cluster, and a
cluster-by-attribute heatmap. No chart library: the engine has no frontend build,
the UI is server-rendered (ADR 0014), and an artefact that lives in `results/` next
to the JSON it came from can be diffed in a way a page cannot. Since a figure cannot
be eyeballed from a terminal, every property a reader would check by looking is
asserted in tests instead: treemap areas proportional to sizes, no overlaps,
containment in the canvas, hull vertices that are real cluster points, row
normalisation, and well-formed XML.

**Finding 1. The heatmap confirms the object view is object-driven, which is the
first direct evidence for it rather than an inference from a score.** On the
constructed set, **31 of 44 clusters contain more than one verb class** — `pick up`,
`place`, `turn` and `screw in` on the same object land together — which is what
clustering by object is supposed to do and what the action view failed to do.

**Finding 2. The colour facet is visible in the figure the way it should be.**
**40 of 44 clusters contain exactly one colour.** The four that mix colours are
where any residual colour risk lives, and they are now identifiable by name rather
than by inference.

**Finding 3. A few clusters are verb-pure instead of object-pure.** Two ten-member
clusters are single-verb (`fold`) clusters. They score perfectly on the pair metrics
— every pair inside them does agree — while being clustered on the wrong attribute
entirely. No scalar in this study would have revealed that; the heatmap shows it in
one glance.

**Finding 4. The treemap caught a fragmentation problem the metrics called a success.**
On the hand set the frozen configuration produces **34 clusters from 46 strings, with
a largest cluster of 2 and 22 singletons** — 78% of clusters are single episodes.
Its pair F1 is still 0.889 with zero false merges, because a fragmentation that
never merges anything is trivially safe on merge-oriented metrics. On the
constructed set the same configuration gives 44 clusters from 273 strings with a
largest of 15, which is a sane clustering. So the configuration is not obviously
wrong; the hand set is 46 strings drawn from mostly distinct real objects and a fine
object view genuinely fragments at that size. **It is a warning, not a verdict** —
but it is the kind of thing that has to be seen before the configuration is shipped,
and it is why the radius is the number to re-check on a real corpus.

**Reproduce.**

```bash
just cluster plot --set constructed --embedding minilm --view object --rule sliding_8 --radius 0.30 --colour-weight 0.5
just cluster plot --set hand --embedding minilm --view object --rule sliding_8 --radius 0.20 --colour-weight 1.0
```

Writes `results/figures/{treemap,map,heatmap}-<encoder>-<view>[-<set>].svg`.

## Not yet run

- MVP integration of the object view into the engine. Unblocked; not started.
- Re-checking the radius on a real corpus, given the fragmentation above.
- A representation trained for this task, which is the only remaining route to the
  action view.
- A learned attribute extractor to replace the colour lexicon, and a decision about
  whether the object view should ignore the verb entirely — which the three
  verb-synonym false splits and the verb-pure clusters are both asking for.