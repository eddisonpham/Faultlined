# Findings — stage 2.5 clustering experiments

## EXP-2.5-01: task-string embeddings do not separate the two gold classes (2026-09-30)

**Question.** Before choosing a clustering method, can sentence embeddings tell a
same-task pair from a different-task pair at all? Every method downstream assumes
yes: if the two classes cannot be separated by distance, no radius and no centroid
rule can separate them, and a factorial over methods would be measuring noise.

**Method.** 50 hand-built pairs, 78 distinct strings, each labelled per view
(`same_action`, `same_object`) with 17 flagged ambiguous and excluded. For each
decided pair, cosine similarity under three encoders. Sweep every candidate
threshold and report balanced accuracy at the best one, because the classes are
unequal in size. Dev and held-out are pooled here: this is a premise check that
reports *where the classes sit*, not a score to be tuned against.

**Result.**

| Encoder | dim | view | pos min | pos mean | neg max | neg mean | best bacc | false merge | false split |
|---|---|---|---|---|---|---|---|---|---|
| `m2v-8m` | 256 | action | 0.415 | 0.730 | 0.836 | 0.518 | **0.682** | 0 | 14 / 22 |
| `m2v-8m` | 256 | object | 0.640 | 0.785 | 0.855 | 0.487 | **0.781** | 7 | 0 |
| `m2v-32m` | 512 | action | 0.427 | 0.735 | 0.806 | 0.506 | **0.683** | 2 | 11 / 22 |
| `m2v-32m` | 512 | object | 0.620 | 0.771 | 0.828 | 0.485 | **0.808** | 4 | 4 |
| `null` (no semantics) | 64 | action | 0.632 | 0.746 | 0.671 | 0.541 | **0.750** | 0 | 11 / 22 |
| `null` (no semantics) | 64 | object | 0.333 | 0.689 | 0.920 | 0.604 | **0.640** | 11 | 1 |

**Finding. The classes interleave at every threshold, on both views, for all
three encoders.** There is no radius that separates same-task from different-task.

**Finding. The semantic encoders are not measurably better than hashing tokens.**
`m2v-8m` scores 0.682 on the action view; the `null` backend — a hash of tokens
with no semantics at all — scores 0.750. Quadrupling the parameters
(`m2v-32m`) changes the action view by 0.001.

**Why, in hindsight.** The pairs differ in *which token appears*, and a static
embedding is a weighted average of token vectors. That is dominated by lexical
overlap, which is exactly the signal that is uninformative here: "pick up the red
cube" and "push the red cube" share four of five tokens and score 0.836, while
"pick up the red cube" and "grab the red cube" are the same task and score 0.87
or lower depending on the pair. The encoder smooths away the one distinction the
label depends on.

**What this does not say.** 37 decided pairs on the action view is a small sample;
balanced accuracy at n=37 carries roughly ±0.12 of noise, so 0.682 versus 0.750 is
**not** a significant gap. The defensible claim is the negative one: *no measurable
advantage from semantics at this sample size*, not *semantics are harmful*.

**Consequences.**

1. **The 30-config factorial is not run yet.** Every configuration would inherit
   these features, so the table would rank methods on noise and invite a confident
   wrong conclusion. This is the specific failure the premise test exists to catch.
2. **The premise behind R1 is not supported** as written in the research record.
   The claim that this is "the rare problem where the right tool is obviously the
   right tool" was made before any measurement and does not survive it.
3. **The two-view design is now load-bearing rather than a refinement.** A single
   embedding cannot separate either view, so the action/object split cannot be a
   nice-to-have layered on top. The learned-projection idea — which *learns* the
   discriminative direction rather than hoping a generic embedding contains one —
   is the remaining hypothesis worth testing, and it is a different experiment from
   "cluster harder".

**Next.** Two hypotheses, both cheap, neither requiring the factorial:
(a) a contextual neural encoder, which can represent "these differ in the verb"
rather than "these share most of their words"; (b) the learned action/object
projection, measured against the same separability metric so it is comparable.

**Reproduce.**

```bash
just cluster separability --embedding m2v-8m
just cluster separability --embedding m2v-32m
just cluster separability --embedding null
```
