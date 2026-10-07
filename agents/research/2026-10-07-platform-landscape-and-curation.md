# Platform landscape and the curation layer — what comparable teams build, and where Faultlined stands

**Date:** 2026-10-07 · **Status:** research complete; the idea set derived from it is in
[2026-10-07-curation-idea-improvements.md](2026-10-07-curation-idea-improvements.md) · **Method:** primary sources
(official docs, papers, repositories) fetched on 2026-10-07; vendor marketing used only where it is
the only description of a product, and labelled as such. Every claim carries a URL and an access
date, per [research rules](README.md).

**Scope note.** This is not a repeat of [industry-patterns.md](industry-patterns.md) (phase 01: how
the industry organises robot ML work) or [infrastructure-gaps.md](infrastructure-gaps.md)
(2026-10-04: history, download and alerting gaps with proposals). Those are read as prior art here.
This document answers a narrower question: **what does the layer between "episodes exist" and "a
training set exists" look like at the companies and projects that do it at scale, and which parts of
it does Faultlined not have?**

---

## 1. The four-layer picture

Across every source below, the same four layers appear, in the same order. Each one is a different
kind of answer, and each has a different failure mode.

| Layer | Question it answers | Industry answer | Faultlined today |
|---|---|---|---|
| **1. Organise** | Where do the bytes live, and how is one episode addressed? | Self-contained or sharded containers with relational metadata indexing episodes inside files | **Has it.** Content-addressed artifacts + `(source_hash, episode_key)` identity; MCAP and LeRobot v3 readers |
| **2. Orchestrate** | What runs, on what compute, in what order? | Declarative workflow graphs over heterogeneous compute (train/sim/edge) | **Partly.** A Postgres queue and a worker loop; no workflow graph, no scheduled pipelines beyond the monitor |
| **3. Curate** | Which episodes should be in the training set, and why? | Embeddings + near-duplicate removal, uniqueness/representativeness scoring, event-driven capture, mixture design | **Has one axis.** Task vocabulary, slices, quality verdicts, validation/quarantine. **No redundancy, no coverage, no "what is missing"** |
| **4. Deliver** | How does a training script consume it? | LeRobot v3 / RLDS / self-contained EBML, plus dataset cards and precomputed stats | **Has it.** LeRobot v3 export of a content-addressed build, read back by our own reader |

The gap is **layer 3**, and it is not a small one: it is the layer the whole industry is currently
investing in. Layer 1, 2 and 4 are commodities — solved, standardised, and in this repository already
implemented. Layer 3 is where the current commercial fight is
([Foxglove data management guide, accessed 2026-10-07](https://foxglove.dev/robotics/data-management-tools-for-robotics-a-2025-guide-comparison);
[Foxglove search + curation launch, cited in infrastructure-gaps.md §2.1](infrastructure-gaps.md)).

---

## 2. Layer 1 — Organise: the container debate is settled, and it is not our problem

**LeRobotDataset v3.0** ([HF docs, accessed 2026-10-07](https://huggingface.co/docs/lerobot/en/lerobot-dataset-v3))
replaced one-file-per-episode with **file-based storage**: many episodes per Parquet/MP4 shard, with
episode boundaries resolved through **relational metadata** rather than filenames, chunked episode
records under `meta/episodes/`, and Hub-native streaming so a training run never downloads the
dataset. Two details matter to us:

- The stated motivation is *lower filesystem pressure* — "fewer, larger files ⇒ faster
  initialization and fewer issues at scale". Our export already writes chunked shards
  ([ADR 0025](../decisions/0025-lerobot-v3-export-of-builds.md)), so we are on the right side of
  this.
- LeRobot now ships a **Lance** variant (`lerobot-lancedb`) as a drop-in storage layout for
  large-scale random-access IO. That is the format question moving under our feet, and it is a
  reason to keep export a *projection of the manifest* rather than a bespoke writer.

**Robo-DM** ([Chen et al., arXiv:2505.15558, accessed 2026-10-07](https://arxiv.org/html/2505.15558v1))
makes the strongest version of the case: robot data management is a *cost* problem as much as a
format problem. Concretely: 8.9 TB of Open X-Embodiment costs **$172/month to store but $172–$1,540
per full download**, and their EBML-based self-contained container reaches **70× lossy / 3.5×
lossless** compression versus RLDS with **50× faster sequential decode** than LeRobot and — the
result that matters — **75× compression with no measured loss in downstream task accuracy**. They
also note LeRobot's own weakness plainly: "its file structure is complex and loading is generally
slower due to decoding".

**Reflection on Faultlined.** We are a **local-first** engine (ADR 0012) whose artifacts are the
source files themselves. Egress cost is not our problem and a new container format is not our
problem. What *is* transferable is the measurement discipline: Robo-DM's headline is not "we are
faster", it is "we are 75× smaller with **no downstream accuracy loss**". That is the shape of claim
this repository requires for any compression or filtering feature
([benchmarking methodology](../benchmarking/methodology.md)). Any curation feature we add must state
its size/quality trade-off the same way or not at all.

---

## 3. Layer 2 — Orchestrate: declarative graphs over heterogeneous compute

**NVIDIA OSMO** ([GitHub, accessed 2026-10-07](https://github.com/NVIDIA/OSMO);
[developer page](https://developer.nvidia.com/osmo); [NVIDIA blog, 2024-03-18](https://developer.nvidia.com/blog/scale-ai-enabled-robotics-development-workloads-with-nvidia-osmo/))
is the reference implementation of the layer above us. Its framing is the **"Three Computer
Problem"**: physical-AI work needs training GPUs, simulation compute and edge/hardware-in-the-loop
devices orchestrated together, and OSMO expresses that as **one YAML workflow** whose tasks carry
`platform:` (GB200, RTX PRO 6000, Jetson AGX Thor), `inputs:` (upstream task outputs), and
`outputs:` (object-storage URLs). Everything else follows: plug-and-play backends, a central control
plane, zero-downtime scaling, and a documented roadmap for **load-aware multi-backend scheduling**
and **high-performance data caching**. It is production infrastructure — it runs GR00T, Isaac Lab,
Isaac Sim and Isaac ROS, "thousands of GPU-hours daily".

Note what OSMO does *not* have: any notion of episode quality, near-duplicates or curation. It moves
data; it does not judge it. Its "Working with Data" card is "transform and post-process data for
iterative improvement" — the curation judgement is left to whoever writes the task.

**Reflection on Faultlined.** Two lessons, one a confirmation and one a caution.

- *Confirmation:* the workflow graph is the industry answer to orchestration, and our ADR 0010
  (modular monolith + out-of-process workers) is the deliberate small-scale version of it. The
  program-audit's note that "the platform has no periodic-execution primitive at all" was the root
  cause of a release blocker, and ADR 0031 fixed it with the smallest possible thing (a loop plus an
  advisory lease) rather than a scheduler dependency. That is the right trade for a single host. A
  workflow DAG is **not** a gap to close here; it is a gap to name.
- *Caution:* the parts of OSMO that are genuinely valuable (load-aware scheduling, caching) are
  valuable *because* the fleet is heterogeneous. Before importing anything from this layer, the
  question is whether our deployment has more than one compute class. Today it does not (ADR 0012,
  one host, optional GPU telemetry). Building an orchestrator would be copying a product rather than
  answering a defect — exactly what the scope guard forbids.

---

## 4. Layer 3 — Curate: where the industry is actually spending money

This is the layer with the most agreement and the most variation. Five distinct sub-ideas recur.

### 4.1 Near-duplicate and semantic-duplicate removal

**SemDeDup** ([Abbas et al., arXiv:2303.09540, accessed 2026-10-07](https://arxiv.org/html/2303.09540v3))
is the canonical formulation: embed every item with a pretrained model, cluster with k-means, and
within each cluster remove pairs whose **cosine similarity exceeds a threshold**, keeping one. It is
deliberately mechanistic — no labels, no model training, a knob you can turn — and the reported
result is roughly **50 % of the data removed with no loss in downstream performance**.
It has been productised: **NVIDIA NeMo Curator** ships it as a curation stage
([NeMo semdedup docs, accessed 2026-10-07](https://docs.nvidia.com/nemo-framework/user-guide/25.07/datacuration/semdedup.html)),
and Nvidia frames it as "removing redundant data from large datasets by identifying and eliminating
semantically [duplicate] items".

**FiftyOne Brain** ([Voxel51 docs, accessed 2026-10-07](https://docs.voxel51.com/brain/)) is the
vision-data version, and the more instructive one, because it decomposes "is this dataset good?" into
seven separate named measures rather than one score:

- **exact duplicates** (file hashes),
- **near duplicates** (embedding similarity),
- **leaky splits** (duplicates *across* train/eval — "an overly optimistic measure for the quality of
  training"),
- **uniqueness** (how much an item differs from everything else; "operates on raw images and does not
  require any prior annotation", useful when deciding *what to label*),
- **representativeness** (which items are typical, which are outliers),
- **mistakenness** and **hardness** (label errors and model-difficulty, both requiring model output —
  explicitly the active-learning half).

Their framing sentence is the thesis of the whole field: these methods "transform how you curate
your data **from an art into a measurable science**". The applications they list are the operator
verbs: find similar examples to a failure case, mine a data lake to fix an issue, recommend what to
add next.

### 4.2 Dataset composition and mixture design

**Open X-Embodiment** ([robotics-transformer-x.github.io](https://robotics-transformer-x.github.io/);
[arXiv:2310.08864](https://arxiv.org/html/2310.08864v9) — both accessed 2026-10-07) pooled **60
datasets from 34 labs, 22 embodiments, 527 skills, 2.4 M+ trajectories** — and then did the thing
that matters here: **analysed diversity** ("the number of visually diverse scenes") as the variable
that predicts positive transfer, and found that mixtures of *fewer* datasets can outperform larger
mixtures. Mixture composition is a first-class result, not an afterthought.

**DROID** ([droid-dataset.github.io](https://droid-dataset.github.io/);
[arXiv:2403.12945](https://arxiv.org/abs/2403.12945) — accessed 2026-10-07) took the opposite route:
**76 k trajectories / 350 h / 564 scenes / 84 k tasks**, made diverse *by construction* — a
standardised rig, many operators, many environments, and a collection app that prompts scene and
task variation. Curation as experimental design rather than post-hoc filtering.

### 4.3 Event-driven capture (select before you store)

**Heex**, as described by Foxglove's comparison
([accessed 2026-10-07](https://foxglove.dev/robotics/data-management-tools-for-robotics-a-2025-guide-comparison)),
records only *relevant* moments, "reducing storage by 90 %+", with remotely adjustable capture
rules. **ReductStore** takes the adjacent position: volume-based FIFO retention so an edge device
cannot overflow, and "conditional replication using custom query language" to cut bandwidth. The
broader guide recommends the standard tiering: hot (0–7 days, local SSD), warm (7–90 days, object
storage), cold (90+ days, archive).

### 4.4 The feedback loop

Tesla's "Data Engine", as described in the widest-cited secondary account of the 2019 AI Day talk
([Karpathy talk summary, accessed 2026-10-07](https://kargarisaac.medium.com/active-learning-data-selection-data-auto-labeling-and-simulation-in-autonomous-driving-part-4-dc985e2c83f9);
Tesla's own framing at [tesla.com/AI](https://www.tesla.com/AI): "evaluate your algorithms at the
scale of the entire fleet") is the loop rather than the filter: fleet → trigger conditions → mining
→ auto-labelling → human verification → retrain. **Flagged as secondary sourcing**: this is a
third-party summary of a talk, not a primary engineering document, and it should be cited as
industry folklore rather than as measured practice. The idea it carries is nonetheless the one
FiftyOne's hardness/mistakenness APIs implement properly, and the one this repository's monitoring
subsystem is the *host* for.

### 4.5 Dataset cards / descriptive metadata

LeRobot v3 carries `meta/info.json` (schema, fps, path templates) and `meta/stats.json` (global
feature statistics, exposed for normalisation) plus `meta/tasks.jsonl`. The export already writes
these ([ADR 0025](../decisions/0025-lerobot-v3-export-of-builds.md)). What no container in this list
carries is a **statement about what the dataset does *not* cover**.

**Reflection on Faultlined — the honest comparison.**

What we already have, and should not rebuild:

| Industry capability | Faultlined equivalent | Verdict |
|---|---|---|
| Semantic/exact dedup of *inputs* | `(source_hash, episode_key)` identity: re-ingesting an episode is a no-op (failure mode F26) | **Done, and stronger than a similarity threshold** — it is exact and free |
| Task/behaviour taxonomy | Task vocabulary with human-approved entries, mappings, and an unmapped queue that is a query ([ADR 0029](../decisions/0029-task-vocabulary-first.md)) | **Done, and rarer than dedup** |
| Quality scoring per item | Movement / jerk / stall / temporal integrity / verdict, with the honesty rules of [ADR 0023](../decisions/0023-quality-metrics-honesty.md) | **Done, deterministic, no model** |
| Selection mechanism | Slices with recomputed membership + a slice impact view that attributes every drop | **Done** |
| Delivery | LeRobot v3 export of a content-addressed build | **Done** |

What we do **not** have, in the industry's own vocabulary:

1. **Near-duplicate / redundancy detection.** Our dedup is *exact* (same bytes) and *textual* (same
   task string, via vocabulary). Nothing detects two episodes that are the same *behaviour* recorded
   twice — a re-teleop, a repeat of the same demo, the same file re-encoded. FiftyOne calls this near
   duplicates; SemDeDup calls it semantic duplicates; both treat it as the highest-value cheap
   curation win.
2. **Uniqueness / representativeness.** Nothing tells an operator which of their episodes are
   informative and which are one of forty near-identical takes.
3. **Coverage / what is missing.** Vocabularies tell you what tasks exist; nothing tells you that a
   *build* covers 4 of 9 robots, or that the episodes it drops are precisely the only ones exhibiting
   a behaviour.
4. **A record of curation decisions.** Slices record membership; nothing records *why* an episode was
   left out, in a form that can be diffed between two builds.

Items 1–3 are the layer-3 gap. Item 4 is downstream of it.

---

## 5. Layer 4 — Deliver: two camps, and the standard is winning

Foxglove's positioning is "MCAP as the log format, browser-based visualisation and search as the
product" ([guide, accessed 2026-10-07](https://foxglove.dev/robotics/data-management-tools-for-robotics-a-2025-guide-comparison);
[Rerun-vs-Foxglove, accessed 2026-10-07](https://foxglove.dev/robotics/rerun-vs-foxglove): "Rerun is
code-first; Foxglove is a full platform + SDK"). Rerun took the opposite route and added
**experimental MCAP support in 0.25** ([Rerun blog, accessed 2026-10-07](https://rerun.io/blog/introducing-experimental-support-for-mcap-file-format)),
i.e. the visualisation tool adapted to the log format rather than the reverse. On the training side,
LeRobot v3 and RLDS are the two serious destinations, with Robo-DM an argument that both are wrong.

**Reflection on Faultlined.** Our export targets LeRobot v3, which is the default in the
manipulation ecosystem and the format the fixtures already use. The relevant observation is not
"pick a different format" but "**the format that wins is the one whose metadata is relational**"
(LeRobot v3's own stated rationale, and Robo-DM's opposite bet). Both camps agree that episode
identity should not be inferred from filenames. Our catalog is already relational and our export
already writes episode offsets — this layer is closed, and the only live question is whether a
Lance/Parquet-layout variant becomes worth a second export adapter. Not now: no requirement in
[requirements.md](../spec/requirements.md) asks for it.

---

## 6. Synthesis: the five ideas, and the constraint that changes all of them

| # | Idea | Where it comes from | Cost in the industry | Cost here |
|---|---|---|---|---|
| I1 | Near-duplicate / semantic dedup | SemDeDup, NeMo Curator, FiftyOne | An embedding model, a GPU pass, and a threshold to tune | ? |
| I2 | Uniqueness / representativeness | FiftyOne Brain | Same embedding pass, plus a distance computation | ? |
| I3 | Coverage / mixture composition | OXE, DROID | Manual analysis, or a bespoke notebook per dataset | ? |
| I4 | Event-driven capture / tiering | Heex, ReductStore | Edge agent, remote rule management, object-storage tiers | Not applicable: local-first, and capture already happened |
| I5 | Curated-dataset cards | LeRobot v3 metadata (schema + stats only) | Usually not done at all | ? |

**The constraint that changes all of them.** Every industry implementation of I1/I2/I3 assumes one
of: an embedding model, a GPU, a network, or a service. Faultlined's decisions forbid all four in a
decision path — [ADR 0020](../decisions/0020-deterministic-monitoring-notifier.md) (no model, no
LLM, no network in detection), [ADR 0014](../decisions/0014-minimal-ui-server-rendered.md) (stdlib
UI, no bundler), [ADR 0012](../decisions/0012-host-based-development.md) (one host),
[ADR 0011](../decisions/0011-coverage-gate.md) + the dependency policy in
[CLAUDE.md](../../CLAUDE.md) ("every dependency must earn its place"). The engine's runtime
dependency set is seven packages and contains no numerical library at all
(`pyproject.toml`: fastapi, uvicorn, pydantic, pydantic-settings, psycopg, pyarrow, mcap, psutil).

So the interesting question is not "can we copy SemDeDup" — we cannot, and copying it would need a
model, a GPU and a dependency, none of which the scope guard or the ADR set permits. The interesting
question is:

> **Is there a deterministic, dependency-free descriptor of an episode that is already computed at
> ingest, from which redundancy, uniqueness and coverage can be derived?**

If the answer is yes, then this repository can have the industry's layer 3 **for free** — as a
property of ingest rather than as a curation job someone has to remember to run, on a single host,
with no model, and fully explainable (a human can read why two episodes were called duplicates).
That is the improvement the next document specifies, and the measurement that decides whether it is
good.
