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

**Result.** Balanced accuracy at the best threshold, and the supervised ceiling:

| Encoder | kind | dim | action bacc | action ceiling | object bacc | object ceiling |
|---|---|---|---|---|---|---|
| `null` | hash of tokens | 64 | 0.750 | 0.660 | 0.640 | 0.564 |
| `m2v-8m` | static | 256 | 0.682 | 0.737 | 0.781 | 0.566 |
| `m2v-32m` | static | 512 | 0.683 | 0.748 | 0.808 | 0.566 |
| `minilm` | contextual | 384 | 0.644 | **0.770** | **0.883** | 0.597 |

**Finding 1. The classes interleave at every threshold, on both views, for all
four encoders.** There is no radius that separates same-task from different-task.

**Finding 2. Capacity is not the lever.** `m2v-8m` → `m2v-32m` moves the action
view by 0.001. Quadrupling the parameters buys nothing.

**Finding 3. Static encoders are dominated by lexical overlap.** A static encoder is
a weighted average of token vectors, so "pick up the red cube" and "push the red
cube" — different actions — score **0.836**, and "push" vs "pick up" negatives sit
at 0.649 mean under MiniLM. A contextual encoder finds those two sentences
genuinely similar, which is true and is exactly what makes the action view hard.

**Finding 4. Contextual helps objects and hurts actions, and the reason
matters.** Under MiniLM the object view reaches 0.883 with the largest gap between
classes anywhere in the study (positives 0.870 mean, negatives 0.543). On the
action view it is the *worst* encoder by threshold score — but it has the
*highest* supervised ceiling (0.770). The action information is present in the
representation and a distance threshold cannot reach it. That is precisely the
case a learned projection is for, and it is the only finding here that points at
a way forward.

**Finding 5. Error direction differs, and one of them is the dangerous one.**
At MiniLM's best threshold the object view produces **0 false merges and 7 false
splits**. A false merge silently mixes two tasks into one cluster and corrupts a
build; a false split shows up as two small clusters that a human merges in seconds.
Measured that way, the object view is usable and the action view is not (10 false
merges at its best threshold, against a 0.770 ceiling).

**Caveats, stated rather than buried.**

- 37 decided action pairs is a small sample; balanced accuracy carries roughly
  ±0.12. Differences of that size are not significant, so the defensible claim is
  the negative one: *no measurable advantage from semantics at this sample size*.
  It is **not** "semantics hurt".
- The supervised ceiling is a rough diagnostic. Its items are not independent —
  the same string appears in many pairs — so a leave-one-out lookup often finds a
  near-identical string from a different pair. That inflates it in principle and
  makes it noisy in practice. It is good enough to say "the ceiling is low
  everywhere" and not good enough to quote as a precise bound.
- The claim in the stage 2.5 research record that this is "the rare problem where
  the right tool is obviously the right tool" was written before any measurement
  and does not survive one.

**Consequences.**

1. **The 30-config factorial is not run.** Every configuration would inherit these
   features. This is the failure the premise test exists to catch.
2. **A pure clustering approach is not viable as designed.** What survives is
   narrower and more honest: MiniLM on the **object** view, as a *proposal* that
   surfaces candidate groups for a human, with the action view unresolved.
3. **The next experiment is the learned action/object projection**, scored with
   this same separability metric so it is comparable. Finding 4 says it is the one
   with a real hypothesis behind it.

**Reproduce.**

```bash
just cluster separability --embedding null      # control, no semantics
just cluster separability --embedding m2v-8m     # static, 256d
just cluster separability --embedding m2v-32m    # static, 512d
just cluster separability --embedding minilm     # contextual, 384d
```

## Not yet run

- `centroids` — built and passing, but its radius was chosen arbitrarily before the
  premise test existed and the features it ranks on do not separate the classes.
  Held until a representation does.
- the method × view × encoder factorial, for the same reason.
- the manual boundary audit, which needs a finalist.