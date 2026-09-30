# Stage 2.5 — Where AI/ML earns its place in the pipeline

Research date 2026-09-30. Sources in [source-log.md](source-log.md) #48–#56.
Status: **analysis only.** No ADR, no code, no dependency added. Recommendations below are
proposals for the owner to accept, revise, or reject.

## 0. The bar

Per CLAUDE.md and the ADR-before-architecture rule, an integration lands only if it **solves a
real problem this project actually has**, is not a duplicate of hand-perception / VLA-quantization /
grasp-deployment work, and carries a justification that survives the question *"what breaks if we
delete it?"* Anything added because a technology exists, or because a vendor case study says so,
is rejected below on those grounds.

Three constraints from the existing design do most of the deciding, so they come first.

| Constraint | Source | What it forbids |
|---|---|---|
| Models are workloads we execute, not our product | CLAUDE.md scope guard | Anything requiring a *policy* to be trained or held in-process |
| Must run with GPU absent, ≤ 8 GB VRAM | NFR-007, [environment](../spec/environment.md) | A VLM in the ingest or validate path; 27B-class serving |
| Build determinism: identical content hash on rebuild, 100% or release fails | NFR-004 | Anything unseeded or version-unpinned inside a build's content hash |

The captured hardware is a single RTX 5060 with 8 GB VRAM, a 24-core CPU, 31.4 GB RAM, Windows,
no Docker. The dependency list today is nine packages and contains **no ML framework at all**.
Every proposal below is costed against keeping it that way, or explicitly paying for it.

## 1. What the field actually does

Six techniques appear repeatedly in robot-data curation work. Sorted by whether they need a
trained policy, because that is the single question that separates "usable by a data engine" from
"belongs to the training pipeline".

| Technique | Reported result | Needs the policy? | Fits 8 GB / CPU? | Verdict here |
|---|---|---|---|---|
| Influence-function curation (CUPID, QoQ) | 99.4% curation accuracy vs. heuristic baseline | **Yes** — requires gradients/Hessian of the training run | No | **Reject** |
| Mutual-information curation (DemInf) | Rewards demos by MI to the learned policy | **Yes** | No | **Reject** |
| Self-supervised pruning (SCIZOR) | Removes suboptimal transitions + redundant pairs; policy-agnostic, no reward | No | Lightweight self-attention encoder | **Candidate** |
| VLM episode annotation (`lerobot-annotate`) | Writes subtasks/plans/VQA into LeRobot v3.1 columns | No | Default model is 27B on an h200 | **Candidate, constrained** |
| VLM failure detection w/ uncertainty (EMBS) | Runtime failure classification with MC uncertainty | No | Needs a served VLM | **Defer** — runtime, not data engine |
| Learned time-series anomaly detection | Vendor claims of 60–99% fewer false positives | No | CPU | **Blocked** — see §4 |

Two findings matter more than the table.

**The accuracy claims come with a hidden coupling.** The two best-performing methods
(99.4% curation accuracy) are the two that require training and differentiating the policy you are
about to train. A data engine cannot do that: it must serve *any* consumer's policy, and holding a
policy in-process is exactly the scope-guard violation. The methods that survive that filter are
the self-supervised ones.

**LeRobot already ships a VLM annotator.** `lerobot-annotate` watches each episode's video and
writes `language_persistent` / `language_events` back into the v3.1 parquet. This is decisive in
two ways: the format Faultlined reads and writes has an official AI-assisted metadata path, and the
upstream project validated the shape of the problem (episodes arrive without a description of what
they depict, and a downstream VLA needs one).

## 2. The hardware wall

`lerobot-annotate` defaults to `Qwen3.6-27B` served by vLLM and, when that does not fit, dispatches
itself to a Hugging Face Jobs `h200`. We have 8 GB. There is no local configuration of that model.

So **a VLM in the ingest or validate path is off the table** — not as a preference, but because it
cannot run, and because NFR-007 requires the GPU-absent path. What survives is *text-only work over
metadata we already have*, which needs no episode bytes and no vision at all. That is a much smaller
opportunity, and the rest of this document is about whether the smaller opportunity is real.

## 3. The diagnosis: which Faultlined gaps are genuinely AI-shaped

I went looking for a problem in *our* pipeline that a model solves better than a rule, and rejected
my own first three candidates. The ones that survived:

### 3.1 Task-string fragmentation — measured, industry-scale, and a silent failure

A survey of real LeRobot v3 datasets found **23,559 distinct task strings across 27,500 episodes**
— roughly one unique string per episode. Operators write "pick up the red cube", "pick up red cube",
"grab the red block", and each becomes its own value.

This is a *silent* failure, which is what makes it worth fixing rather than documenting. A
downstream ML engineer writes a filter — `task == "pick up the red cube"` — and it matches two
episodes instead of six hundred. Nothing errors. The build succeeds, the run record cites it, and the
policy is quietly trained on a twentieth of the intended data. No amount of correctness in ingest,
validation, or lineage catches this, because every layer above it is faithfully preserving a value
that was never canonical to begin with.

It is also the rare problem where the right tool is obviously the *right* tool: mapping free text to
a canonical vocabulary is semantic similarity, which is what text encoders are for.

### 3.2 No near-duplicate detection

Faultlined's content-addressed artifact store catches *byte-identical* episodes and nothing else.
Re-recording the same task with slightly different lighting, timing, or a 2% action offset produces a
new hash, a new catalog row, and a build that counts the same demonstration twice. Content addressing
answers "is this the same file?", which is the easy half of the question.

This maps onto SCIZOR's second mechanism (cluster embeddings to find redundant pairs) and onto
standard large-scale dedup practice (BigCode, NeMo Curator). Unlike §3.1 it needs an encoder over
sensor data, not text.

### 3.3 The stated job-to-be-done has no interface for it

The robotics-data-engineer persona in [problem.md](../spec/problem.md) is given *"Turn today's robot
recordings into trusted catalog entries."* Foxglove — a direct competitor in source-log #10/#21/#22 —
now ships exactly the missing half: an agentic layer with semantic search over multimodal robotics
data. The gap between what our personas are described as needing and what the API can express is
this: `state=quarantined AND flag=jerky` is the entire query language.

## 4. What I rejected, and why

**Influence functions / MI curation.** Best reported accuracy, but policy-coupled. Rejected on the
scope guard, not on accuracy.

**Learned anomaly detection in the notifier.** Tempting, because the notifier has 11 rules on static
baselines and the vendor literature promises large false-positive reductions. Rejected for two
reasons. First, those numbers are marketing: the sources found make claims like "reduces false
positives by 60–80%" with no public methodology. Second, and decisively, the field's own
evaluation methodology is known to be broken — Wu & Keogh (TKDE 2021, ~594 citations) showed current
time-series anomaly-detection benchmarks are so flawed they create the illusion of progress. To add
a learned detector here we would need labelled faults *in our own system* to measure recall and
precision against. That label set is what the B-016 chaos campaign is supposed to produce, and it has
never been run.

**This makes B-016 a prerequisite, not a parallel workstream.** That is the single most useful
conclusion of this research: a chunk of stage 2.5 is *blocked* on a stage-5 deliverable we already
scoped. Running the chaos campaign first is what unlocks it.

**LLM-authored validation profiles.** Tempting — the profiles are hand-written JSON today, and
generating one from "reject episodes with fewer than 1000 frames" is easy. Rejected on the same
principle as the anomaly detector: validation is a *gate*, and putting an unverified model behind a
gate is a bandaid that looks like rigor. A model-proposed rule can be reviewed and promoted to a
declared profile; it cannot be the gate itself. Same reasoning applies to auto-quarantining.

**Anything generative, in a build's content hash.** NFR-004 requires identical hashes on rebuild.
An unseeded or unpinned model call inside a build would make builds irreproducible, breaking the one
hard NFR the project treats as release-blocking.

## 5. The one rule that keeps this from becoming a bandaid

Learned from `lerobot-annotate`'s own architecture, which stages output, validates it, and writes
through a single writer:

> **AI output is a proposal with provenance. It is never a gate and never mutates state silently.**

Concretely, every integration below must:
- run as an ordinary `JobType` through the existing lifecycle, writing to the content-addressed
  artifact store like anything else — never a special path into the catalog;
- record model id, version, prompt, and seed in the result, so a build that depends on it is
  auditable and NFR-004 can still hold;
- land as *metadata a human can filter on*, leaving declared validation rules authoritative;
- degrade to a no-op when the model or its weights are absent, with the pipeline fully functional —
  which is what NFR-007 already requires of everything else.

If a proposal cannot satisfy all four, it is out.

## 6. Recommendations, in order

### R1 — Canonical task vocabulary (do this first)

**Problem (§3.1):** near-unique task strings make metadata filters silently wrong.
**Approach:** embed each distinct task string with a small sentence encoder, cluster, and *propose*
cluster labels for a human to confirm. Then store `canonical_task` as a first-class field alongside
the original, never replacing it.
**Why this tool:** the task is *clustering*, not generation. Generation would invent canonical
names a human must then audit anyway; clustering only proposes groups, and the human supplies the
name. Nothing is hallucinated, and a wrong grouping is visible rather than silently applied.
**Cost:** a MiniLM-class encoder is ~23 M parameters and a few thousand strings encode in
milliseconds on CPU. Text only — no episode bytes, no GPU, no vision.
**Degrades:** without the encoder, `canonical_task` is simply absent and everything behaves as today.
**Falsifiable test:** take a real dataset, cluster its task strings, and measure how many exact-match
filter pairs the clustering would have caught. If clustering is near-random on real operator text,
stop — the premise is wrong and this costs one afternoon to find out.

**The open dependency question, stated honestly:** the cheapest correct path is a pure-Python static
embedding model, avoiding PyTorch entirely. PyTorch would add ~2 GB of install and a CUDA build to a
project that currently has no ML framework, on a Windows host. That is a real cost against the
project's "every dependency must earn its place" rule, and it should be an explicit owner decision
rather than something an implementation quietly assumes.

### R2 — Natural-language episode search (same dependency, second increment)

**Problem (§3.3):** the query language is hand-built flags; personas need "find me the episodes where
the placement failed".
**Approach:** translate a natural-language request into the existing structured filter over
metadata and quality signals. Crucially, it *compiles to the current query language* rather than
inventing a new execution path — so it inherits the same 1..500 bound, the same predicates, and the
same lineage.
**Depends on:** R1's encoder. Nothing to build until R1 is measured.
**Reject if:** it cannot express most real requests as a composition of existing predicates, because
then it is a second query language to keep correct.

### R3 — Near-duplicate detection (gated on an evaluation set)

**Problem (§3.2):** content hashing catches byte-identical episodes only.
**Approach:** SCIZOR-style — a small self-supervised encoder over downsampled state, embeddings
clustered to flag redundant pairs, surfaced as a *flag* alongside the existing `jerky`/`stalled`
views. Never a delete; always a proposal, per §5.
**The nice property that makes this evaluable:** a labelled duplicate set can be *constructed*
rather than collected — take real episodes and apply known perturbations (time shift, 2% action
noise, re-encode, truncate) to produce near-duplicates with exact ground truth. Unlike quality
scoring, this has no dependency on human labels, so it can be measured before it is trusted.
**Blocked on:** that evaluation set existing, and on R1's dependency decision. Do not build the
detector first.

### R4 — Learned quality scoring (blocked, do not start)

The natural pull is to replace the hand-coded `movement_score` / `jerk_score` / `stall_ratio` with a
learned progress model, per SCIZOR's first mechanism. **Blocked, and the blocker is fundamental:**
training it needs labels of which transitions actually made progress, and we have none. Those labels
come from either the B-016 chaos campaign or VLM annotation — and VLM annotation is off the table per
§2. This is deferred until a label source exists, not scheduled before it.

## 7. What would change my mind

- If R1's clustering is near-random on real operator text, the premise of §3.1 is wrong and R1/R2 die.
- If a MiniLM-class encoder cannot be run dependency-light on Windows, R1's cost rises enough to
  re-justify not doing it.
- If the B-016 chaos campaign yields a labelled fault set, R4 unblocks immediately and outranks R3.
- Every vendor accuracy claim in §1 is unverified by us. Per the project's own benchmarking
  methodology, **no number in this document is a project result** — they are literature readings that
  motivate a measurement, not measurements.

## 8. Sequencing

```text
B-016 chaos campaign  ──unblocks──▶  R4 learned quality scoring
        (stage 5, already scoped)

R1 canonical task    ──enables──▶   R2 natural-language search
        │
        └──decision on the ML dependency──▶  R3 near-duplicate detection
                                            (needs the constructed eval set first)
```

Nothing here blocks the MVP, and nothing in stages 3–6 should wait on it except R4, which is
explicitly waiting on a stage-5 deliverable. The honest headline: **of the four recommendations, two
are cheap and unblocked, one is cheap but needs an evaluation set, and one cannot honestly be
scheduled at all yet.** That ratio is better than the "add a VLM to the pipeline" framing this kind
of request usually arrives with.
