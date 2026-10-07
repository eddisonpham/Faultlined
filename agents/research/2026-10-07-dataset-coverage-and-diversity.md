# Round 2: what the industry measures about dataset coverage, and what it implies here

**Date:** 2026-10-07 · Second pass of the curation programme, read against the tree at `4c4e2f2`.
Companion to [round 1](2026-10-07-platform-landscape-and-curation.md); the candidate set this feeds
is in [the idea document](2026-10-07-curation-idea-improvements.md).

**Why a second pass.** Round 1 named four layers (Organise / Orchestrate / Curate / Deliver) and built
the first piece of layer 3: a deterministic behavioural fingerprint that answers "are these two of my
episodes the same behaviour recorded twice" ([ADR 0032](../decisions/0032-behavioural-fingerprints.md)).
[EXP-0020](../experiments/0020-curation-ab-end-to-end.md) then walked that claim end to end as an
operator and the answer held — the representatives exported 25 % of the control with every planted
behaviour retained. The experiment also raised the question it could not answer: **the smaller build
is narrower, and nothing in the product says what it gave up.** Round 2 is about that question.

---

## 1. What the literature actually measures

| Source | What it measures | How | What it does *not* do |
|---|---|---|---|
| **AgiBot / Shanghai Innovation Institute, "Is Diversity All You Need for Scalable Robotic Manipulation?"** (arXiv:2507.06219) | Diversity along **three named axes: task (what to do), embodiment (which robot), expert (who demonstrates)** | Controlled pre-training mixtures, downstream transfer, power-law fits | It is a *study*: the axis decomposition is the result, and applying it to a different dataset means writing a different study. Its most useful finding for this repository is that **task diversity outweighs demonstrations per task**, while **expert diversity — repeated takes by different operators, whose velocity profiles differ — is confounding** and is corrected by *removing* it |
| **Open X-Embodiment** (arXiv:2310.08864) | Cross-embodiment diversity: 60 datasets, 34 labs, 22 embodiments, 527 skills, 2.4 M trajectories | Post-hoc aggregation of published dataset statistics | Diversity is described, not *gated*. There is no per-mixture report an operator can consult before deciding what to train on |
| **OpenVLA** (cited from the same paper) | — | Removing one dataset (DROID) *improved* policy performance | Establishes that "more data" is not a scalar that can be maximised; something has to be measured instead |
| **DROID** (arXiv:2403.12945) | 76 k trajectories / 350 h / 564 scenes / 84 k tasks | **Diversity by construction** — a collection protocol, not a measurement | Nothing in the released artefact tells a downstream user which slice of it their own mixture is missing |
| **Dataset Cartography / Data Maps** (Swayamdipta et al., EMNLP 2020) | Per-example difficulty and variability from **training dynamics** across epochs | Train a model several times and record confidence trajectories | Needs a trained model per analysis, i.e. the exact dependency this engine refuses (ADR 0020). It also answers per-example questions, not coverage questions |
| **Croissant / dataset cards / Datasheets** | Dataset metadata: fields, splits, provenance, licence, sometimes a prose "diversity" paragraph | A standard to be *filled in* by the publisher | A card can assert "diverse" and be wrong; it is a claim about a dataset, not a computed property of one. Nothing in the standard compares a *mixture* against what exists |
| **SemDeDup** (arXiv:2303.09540), **FiftyOne Brain** | Redundancy and uniqueness over an embedding space | k-means + cosine threshold; Brain operators | Round 1's subject; closed here by ADR 0032. Brain comes closest to a productised curation measure, and it still needs an embedding pass and a GPU |
| **Robo-DM** (arXiv:2505.15558), **Foxglove's data-management guide** | Storage: container compression, retention tiers, replication | Container format, storage tiering | Storage cost, not behavioural coverage. Already recorded in round 1 |

**The pattern.** Coverage/diversity is measured *once, offline, by the authors of a dataset or a paper*,
and then published as a statistic in prose or in a table. There is no product surface that answers, for
a specific build an operator is about to export, **"what does this contain, and what does it lack"** —
and there is nothing that compares a mixture against the catalog it was drawn from. The closest things
are FiftyOne Brain's representativeness (an embedding, per-dataset) and Croissant's free-text fields
(a claim).

## 2. What this repository already has

Read from the code, not the docs:

| The industry's axis | Where it already lives here | Persisted as |
|---|---|---|
| **task** | `episodes.metadata->>'task'`, normalised against a human-approved vocabulary (`task_vocabulary_entries` / `_mappings`, ADR 0029) | one raw string per episode, plus a mapping and an entry |
| **embodiment** | `episodes.metadata->>'robot'` (readers emit `robot`; `builds/export.py` falls back to `robot_type`) | one string per episode |
| **expert / take** | the repeated recordings round 1's fingerprint collapses | nothing explicit — the descriptor makes them comparable, which is the point (ADR 0032) |
| **format** | `episodes.format` | one string per episode |
| **quality** | `episode_quality.verdict` and the judged dimension set | one row per scored episode |

So the three axes the AgiBot paper spent a study to separate are, in this engine, **already columns on
the episode row**. That is the whole opportunity: the measurement the literature performs offline with
a model and a script can be a *pure aggregation of rows this catalog already holds*.

## 3. What round 2 should build, and on what grounds

**The claim.** Coverage should be a **property of a build**, computed on read, bounded by the number of
axis values rather than by the number of episodes, and reported as an **explicit gap list** rather than
a percentage.

Three decisions follow from the evidence above and are the ones the ADR will record:

1. **Gaps, not scores.** Every source surveyed publishes a scalar or a figure ("2.4 M trajectories, 22
   embodiments"). A scalar cannot be acted on: it says a mixture is *less* diverse, not *what to
   collect*. The actionable object is a set difference — the values the catalog holds and the build
   does not. Nobody surveyed publishes that.
2. **The reference set matters more than the measure.** A gap only means something against a frame of
   reference. Two frames exist here and both are real: **the catalog** ("you curated out every episode
   of the other robot") and **the operator's own vocabulary** ("you named `fold the cloth` and there is
   no episode for it anywhere"). The second is the more interesting one and is only expressible because
   ADR 0029 made the vocabulary a first-class table. This is an advantage no surveyed system has: a
   curated task vocabulary to be *incomplete against*.
3. **Bounded by construction, or it makes audit item #8 worse.** `quality/summary` already ships a
   2.2 MiB payload at 10 k episodes and `/ui/insights` 5.2 MiB ([EXP-0015](../experiments/0015-feature-latency-at-three-scales.md),
   audit item #8). A coverage report that grouped by anything episode-shaped would repeat that mistake.
   Capping per axis and stating the cap is what keeps this feature from being a new instance of the
   problem it is meant to counter.

**The falsifier, stated before running** (as in round 1): if the gap list on real fixtures is empty, or
if it says only "everything is covered", the report is a table nobody reads and the idea should be
dropped rather than polished. A second falsifier is operational: if the report cannot be produced
inside the 200 ms read target at 10 k episodes *with a bounded payload*, the design is wrong, not the
budget.

**What is explicitly not claimed.** This is not a measurement of how well a policy will train — that is
what the AgiBot paper's downstream experiments do, and nothing here can. It is a coverage *report*: a
description of what a build contains, of the same kind as a build's manifest, and like the manifest it
is an input to a decision rather than the decision.
