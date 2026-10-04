# Clustering: methods research and verdict

**Date:** 2026-10-04 · **Status:** research complete, verdict below, ready to proceed
**Question.** The task-string clustering pipeline (extract → hash → online cosine radius → proposals → human confirm) produces nonsensical labels and clusters and demands human intervention. From an *engineering* standpoint — not just methodology — should we change the algorithm, change the pipeline shape, or remove the feature?

Every external claim below cites a URL + access date (research rules in [README.md](README.md)).
Internal measurements cite `experiments/clustering/results/README.md` (EXP-2.5-01…08) and ADR 0026.

---

## 1. What the engine does today, and where it hurts

Pipeline (ADR 0026, `src/data_engine/clustering/`):

1. **Extract.** Lexicon strips the longest known verb phrase, colours, stop words, and site
   prepositions; what remains is the `core` (`extract.py`).
2. **Hash.** The core is token-hashed into a fixed 256-d vector (`online.token_vector`) — no model,
   no dependency (ADR 0026 §8).
3. **Group.** Online cosine-radius clustering with running-mean centroids (`online.py`).
4. **Propose + confirm.** Every group is a proposal with health numbers; a human posts the label
   (`proposals.py`, `/ui/clusters`).

Measured pain points:

| Pain | Evidence | Severity |
|---|---|---|
| Whole-sentence semantics cannot group tasks | EXP-2.5-08: 533 clusters for 48 classes, B-cubed 0.073, ARI −0.0005 across arrival orders — *scale made it worse* | Foundation of the design (extraction is the algorithm) |
| The lexicon is a maintenance surface | Verb-match rate 100% on the strings it was written from, **82% on the full corpus** (EXP-2.5-08 health block; ADR 0026 "Consequences"). Every unseen verb leaves the core intact | High — unrecognised strings fragment silently until the health tile is read |
| Merges are invisible from inside a proposal | ADR 0026 §"Consequences": "a merge is much harder to notice than a split"; `merged_cores` is the only witness | High — this is the "nonsensical cluster" experience |
| Labels only come from humans, and the queue is unbounded | `cluster_review` is a flat bounded queue of candidates; nothing proposes a *name*, so every confirmed label is manual typing | High — "requires human intervention" |
| Proposal identity is content-derived | `proposal_key = blake2b(core)`; changing extraction options re-keys every proposal; confirmations survive only via the >50% string-overlap rule and can go orphaned (ADR 0026 §3–4) | Medium — correct but invisible bookkeeping |

**Conclusion from internal evidence alone:** the *grouping* half of the pipeline was fixed by
extraction (47 clusters / B-cubed 0.986 / 48 of 48 intact on the study corpus). What remains
broken is the **semantic layer around it**: naming, merge detection, and the unbounded human
queue. So the question is not "which clustering algorithm" — it is "what is this feature for:
discover topics, or canonicalise vocabulary?"

---

## 2. What the engineering literature does with this problem

### 2.1 This is entity resolution, not topic modeling

The shape of the problem — "dozens of distinct strings meaning the same handful of activities,
free-form, arriving from humans at collection time" — is the canonical **entity resolution /
canonicalization** workload (deduplication → record linkage → canonical form), not document
clustering ([District Data Labs, 2017](https://districtdatalabs.silvrback.com/basics-of-entity-resolution),
accessed 2026-10-04; [Evensen, 2024](https://medium.com/@adev94/entity-resolution-an-introduction-fb2394d9a04e),
accessed 2026-10-04). The mature engineering stack for it is:

1. **A controlled vocabulary with preferred + alternate terms.** SKOS-style: one preferred label
   per concept, synonym rings map variants onto it
   ([Modern Data 101, SKOS guide](https://www.moderndata101.com/blogs/demystifying-skos-for-practitioners-a-practical-guide-to-controlled-vocabularies),
   accessed 2026-10-04; ANSI/NISO Z39.19 vocabulary-management standard cited in
   [DAMA-DMBOK 2 §1.3.2.1](https://pdfcoffee.com/dama-dmbok-2nd-edition-data-management-body-of-knowledge-pdfdrivecom-pdf-2-pdf-free.html),
   accessed 2026-10-04). "Vocabulary control brings together synonyms under a single concept"
   ([Graphwise](https://graphwise.ai/fundamentals/what-is-taxonomy/), accessed 2026-10-04).
2. **Blocking + match/merge (survivorship) with stable IDs.** ER pipelines narrow candidates with
   blocking, then decide match/merge and produce golden records with stable identity
   ([Faingezicht, 2024](https://faingezicht.com/articles/2024/09/03/entity-resolution/), accessed
   2026-10-04; [Beheshti et al., 2026, MDPI](https://www.mdpi.com/2078-2489/17/9/917), accessed
   2026-10-04).
3. **Semantic similarity only as a *candidate generator*, not a decider.** Semantic dedup tooling
   (NeMo Curator SemDeDup,
   [docs.nvidia.com](https://docs.nvidia.com/nemo/curator/curate-text/process-data/deduplication/semdedup),
   accessed 2026-10-04; [MinishLab/semhash](https://github.com/MinishLab/semhash), accessed
   2026-10-04) uses embeddings to *propose* near-duplicates; the decision is thresholded and
   reviewable.

### 2.2 The robot-data industry already treats task strings as vocabulary to edit

- LeRobot ships `modify_tasks` / `task_replacements` — in-place task-string renaming explicitly
  "for fixing typos, standardizing wording, or re-labeling"
  ([lerobot docs](https://github.com/huggingface/lerobot/blob/main/docs/source/using_dataset_tools.mdx),
  accessed 2026-10-04), and users file bugs asking for exactly this
  ([lerobot#2096](https://github.com/huggingface/lerobot/issues/2096), accessed 2026-10-04).
  `meta/tasks.parquet` is "the canonical home for the task string"
  ([HF Space announcement](https://huggingface.co/spaces/lerobot/robots-that-talk), accessed
  2026-10-04). Task strings are a **first-class, deliberately curated** dimension.
- Dataset-build guides start from a **task taxonomy with canonical templates + paraphrases**
  ("pick up {object} from {location}") held in YAML/JSON *before collection*, frozen after a pilot
  ([Claru, 2026](https://claru.ai/guides/how-to-build-a-language-conditioned-dataset), accessed
  2026-10-04). Naming consistency ("cup" vs "mug" vs "glass") is called out as a curation duty
  ([RoboticsCenter annotation guide](https://www.roboticscenter.ai/blog/robot-data-annotation),
  accessed 2026-10-04).
- Audits of major robot datasets find exactly our failure class: "metadata lies", inconsistent
  labels, and **no per-episode quality validation at any stage**
  ([Traceplane audit of 10 datasets, 2026](https://traceplane.ai/blog/we-audited-10-robotics-datasets),
  accessed 2026-10-04). An engine that canonicalises and validates vocabulary at ingest is solving
  an industry-wide, still-open gap — the feature is worth keeping *if it stops guessing*.
- Even the largest pooled dataset organises skills as a **curated taxonomy, not a clustering**:
  Open X-Embodiment's 527 annotated skills "cluster into canonical categories (pick-and-place,
  push, open, close, grasp)" over 160,266 tasks
  ([EmergentMind OXE overview](https://www.emergentmind.com/topics/open-x-embodiment-dataset),
  accessed 2026-10-04; source paper
  [arXiv:2310.08864](https://arxiv.org/abs/2310.08864), accessed 2026-10-04). At 21 institutions
  the industry chose canonical categories with human annotation — vocabulary-first scales to
  160k tasks; unattended clustering does not.

### 2.3 Clustering-as-topic-modeling is the wrong register (and our data agrees)

BERTopic-style pipelines (embeddings → UMAP → HDBSCAN → c-TF-IDF naming) are the standard modern
topic stack ([BERTopic docs](https://maartengr.github.io/BERTopic/getting_started/clustering/clustering.html),
accessed 2026-10-04), but they assume document-length text and a *topic* goal. Task strings are
3–8 words and the goal is identity ("is this the same activity?"), not themes. Our own EXP-2.5-08
is the empirical version of this mismatch: whole-sentence embeddings shattered at scale while
extraction recovered 48/48 classes. Short-text clustering literature papers over this with
aggregation/generation tricks (e.g. Gibbs-BERTopic hybrid,
[IEEE 2025](https://ieeexplore.ieee.org/iel8/6287639/10820123/10930480.pdf), accessed 2026-10-04)
— moving complexity, not removing it, and adding model dependencies that ADR 0014 forbids.

### 2.4 Labels: LLMs are a good proposer, humans stay the decider

- A controlled study of LLM cluster naming (GPT-3.5, two benchmark corpora) found the best
  prompting strategy **beat human-written cluster names on all quality domains**, with the caveat
  that strategy must be tuned per dataset
  ([Preiss, Arbeit & Berghammer, *J. Data Science* 22(3), 2024](https://jds-online.org/journal/JDS/article/1385),
  accessed 2026-10-04).
- Weak-supervision practice (Snorkel) formalises the general pattern: many cheap noisy labelers,
  a model to denoise them, humans auditing the result
  ([snorkel.ai](https://snorkel.ai/data-centric-ai/weak-supervision/), accessed 2026-10-04).
- Active-learning practice says the review queue should be **prioritised by uncertainty**, not
  exhaustive ([Label Studio active learning guide](https://docs.humansignal.com/guide/active_learning),
  accessed 2026-10-04; SageMaker Ground Truth auto-labeling docs,
  [AWS](https://docs.aws.amazon.com/sagemaker/latest/dg/sms-automated-labeling.html), accessed
  2026-10-04).

ADR 0020 already rejects an LLM *in the decision path* for monitoring (non-reproducible output in
a trust-critical path). The same constraint applies to grouping. But a **label proposer** whose
output is human-confirmed before it becomes a preferred label is a different trust class — it
never decides anything by itself.

---

## 3. Options considered

| Option | What changes | Evidence for | Evidence against | Verdict |
|---|---|---|---|---|
| **A. Tune the current pipeline** (lexicon growth, radius/centroid sweeps) | nothing structural | extraction already gives 48/48 on the study corpus | EXP-2.5-08: no radius recovers whole-sentence embeddings; the pain is naming/merges/queue, which tuning does not touch; lexicon coverage is 82% and every gap fragments silently | **Reject** as the primary fix |
| **B. Swap in a modern clustering stack** (embeddings/UMAP/HDBSCAN, BERTopic-style) | the algorithm | literature standard for topics | Directly contradicted by EXP-2.5-01…08 at our scale; adds model deps (ADR 0014); nondeterminism/order sensitivity measured at ARI −0.0005 | **Reject** |
| **C. Remove the feature** | delete `clustering/`, `/ui/clusters`, cluster tables | removes the nonsensical-label surface entirely; the review queue is a real cost | The problem is real and industry-open (Traceplane audit; LeRobot task tooling; Claru taxonomy guidance). Slices/builds downstream lose the only activity-level dimension. Removal solves the symptom and deletes the value | **Reject** |
| **D. Change the pipeline: canonicalisation first, clustering demoted to candidate generation** | the *role* of clustering and where vocabulary lives | ER literature (§2.1); LeRobot/Claru practice (§2.2); LLM-naming study (§2.4) for labels; keeps ADR 0014 (dependency-free core) and ADR 0020 (no model in the decision path) intact | More moving parts than A; needs a vocabulary store and a mapping-review UX | **Accept — the verdict** |
| **E. D + LLM in the grouping loop** | grouping decisions | LLMs cluster short text well in benchmarks | Nondeterministic grouping in a trust-critical provenance path violates the ADR 0020 principle; EXP-2.5-08 shows confident-and-wrong grouping is the failure we already paid for once | **Reject for grouping; accept for label *suggestion* only (offline, human-confirmed)** |

---

## 4. Verdict: change the pipeline shape, keep extraction, demote clustering

**Restate the feature:** it exists to turn free-form task strings into a *curated activity
vocabulary* that slices, builds, and reviews can trust. Clustering is not the product; it is one
input. Concretely:

1. **The vocabulary is the source of truth, not the partition.** Introduce a task-vocabulary
   concept: each entry has a stable ID, a **preferred label** (human-approved), and **alternate
   strings** mapped onto it. This is the SKOS/synonym-ring pattern (§2.1) and the LeRobot
   `modify_tasks`/`task_replacements` pattern (§2.2). Confirmed clusters already approximate this
   (`cluster_confirmations` stores task strings, ADR 0026 §3); the change is to make the mapping
   the primary object and the proposal secondary.
2. **Ingest canonicalises; clustering only proposes new entries.** At ingest, a task string is
   matched against the vocabulary (exact → extracted-core match, the machinery we already have).
   Unmatched strings accumulate in an **unmapped queue** — this replaces the unbounded review
   queue with a bounded, novelty-only one. No cluster rebuild can relabel anything.
3. **Cluster proposals become synonym candidates.** The existing extract+hash+radius machinery is
   repurposed: instead of "here are N groups, name them", it answers "these unmapped strings
   probably belong to existing vocabulary entry X" or "these k unmapped strings look like one new
   entry — here is a suggested preferred label". Grouping quality no longer decides anything;
   it ranks a queue. The measured extraction quality (48/48 on the study corpus) is exactly what
   candidate generation needs.
4. **Labels are proposed, humans approve.** A suggested preferred label is generated deterministically
   first (the majority core of the candidate group — zero dependencies, reproducible), with an
   optional offline LLM pass per §2.4 behind the same confirm gate. Nothing auto-labels. This
   directly attacks "nonsensical labels": a label can no longer be typed ad hoc against a fragment;
   it names a vocabulary entry with visible members and counts.
5. **Merges become explicit and reversible.** The invisibility of merges (§1) ends when mapping is
   the primary object: an operator merges two vocabulary entries or splits one, and the operation
   is an event with a record (the same >50% orphan rule from ADR 0026 §4 applies to mappings).
6. **Coverage stays loud.** Keep the health block (verb rate, distinct cores, empty rate, orphaned
   count). Unmapped share becomes the headline number: the engine's job is to drive it down
   without guessing.

### Why this is the engineering answer, not just a methodology one

- **Determinism where it matters:** vocabulary mapping is exact/extracted-core matching — fully
  reproducible, hash-stable IDs (like `proposal_key`, but over a curated object that does not
  re-key when options change).
- **Dependency-free core preserved** (ADR 0014): matching and candidate ranking are pure Python.
  The LLM proposer is optional, offline, and non-authoritative.
- **The human queue shrinks from "name every cluster forever" to "decide novel strings"**, the
  standard active-learning shape (§2.4). New corpora produce new vocabulary entries; steady-state
  corpora produce almost no work — which is the definition of "does not require human
  intervention" at scale.
- **It fixes the industry-identified gap** (Traceplane: no validation/curation stage exists
  anywhere in the wild) instead of competing with BERTopic on turf our own measurements say is
  quicksand.

### What would falsify this verdict

- If unmapped share stays high (>20%) after a vocabulary seeded from one real corpus, extraction
  is the bottleneck, not structure — revisit option A's lexicon work first.
- If operators confirm synonym candidates at <50% precision, candidate ranking (not structure) is
  wrong — re-run the EXP-2.5-style measurement with pairs drawn from the real unmapped queue
  before building anything else.
- If a corpus arrives with genuine open-set tasks at high rate (constant novelty), the queue stops
  shrinking and the feature degenerates to labeling work; that is a product decision (adopt a
  Claru-style canonical-template collection protocol upstream) not an algorithm decision.

---

## 5. Sketch of the change (for the eventual implementation record)

- New object: `task_vocabulary_entries` (id, preferred_label, notes) + `task_vocabulary_mappings`
  (task_string → entry_id, provenance: `ingest` | `confirm` | `merge`, timestamps).
- Ingest path: map-or-enqueue; episode rows carry `task_string` unchanged (audit trail preserved;
  mapping is a view, mirroring ADR 0026 §3's claim-shaped confirmation design).
- `/ui/clusters` becomes `/ui/vocabulary`: unmapped queue (novelty-ranked), synonym candidates per
  entry, merge/split actions, health tiles (unmapped share, orphaned mappings, pending
  candidates). Rebuilds stop deleting anything.
- Clustering package shrinks to `extract` (kept as-is — measured) + a ranker; `online.py`
  centroids stay only if the ranker needs them.
- Tests: mapping round-trips, rebuild idempotence (the ADR 0026 §3 bug class), orphan rule
  propagation, unmapped-queue priority order.

## 6. Sources

| # | Source | URL | Accessed | Retrieval |
|---|---|---|---|---|
| 1 | District Data Labs, Basics of Entity Resolution | https://districtdatalabs.silvrback.com/basics-of-entity-resolution | 2026-10-04 | snippet |
| 2 | Evensen, Entity Resolution — An Introduction | https://medium.com/@adev94/entity-resolution-an-introduction-fb2394d9a04e | 2026-10-04 | snippet |
| 3 | Faingezicht, Roughly Everything You Need to Know About ER | https://faingezicht.com/articles/2024/09/03/entity-resolution/ | 2026-10-04 | snippet |
| 4 | Beheshti et al., Entity Resolution Using Transformer-Based LMs (MDPI 2026) | https://www.mdpi.com/2078-2489/17/9/917 | 2026-10-04 | snippet |
| 5 | Modern Data 101, Demystifying SKOS (controlled vocabularies) | https://www.moderndata101.com/blogs/demystifying-skos-for-practitioners-a-practical-guide-to-controlled-vocabularies | 2026-10-04 | snippet |
| 6 | Graphwise, What is Taxonomy? (synonym rings) | https://graphwise.ai/fundamentals/what-is-taxonomy/ | 2026-10-04 | snippet |
| 7 | DAMA-DMBOK 2, §1.3.2.1 Vocabulary Management (ANSI/NISO Z39.19) | https://pdfcoffee.com/dama-dmbok-2nd-edition-data-management-body-of-knowledge-pdfdrivecom-pdf-2-pdf-free.html | 2026-10-04 | snippet |
| 8 | NeMo Curator, Semantic Deduplication | https://docs.nvidia.com/nemo/curator/curate-text/process-data/deduplication/semdedup | 2026-10-04 | snippet |
| 9 | MinishLab/semhash | https://github.com/MinishLab/semhash | 2026-10-04 | snippet |
| 10 | LeRobot, using_dataset_tools.mdx (modify_tasks / task_replacements) | https://github.com/huggingface/lerobot/blob/main/docs/source/using_dataset_tools.mdx | 2026-10-04 | snippet |
| 11 | LeRobot issue #2096 (rename recorded tasks) | https://github.com/huggingface/lerobot/issues/2096 | 2026-10-04 | snippet |
| 12 | HF Space robots-that-talk (tasks.parquet is canonical) | https://huggingface.co/spaces/lerobot/robots-that-talk | 2026-10-04 | snippet |
| 13 | Claru, How to Build a Language-Conditioned Robot Dataset | https://claru.ai/guides/how-to-build-a-language-conditioned-dataset | 2026-10-04 | fetched |
| 14 | RoboticsCenter, Robot Data Annotation (naming consistency) | https://www.roboticscenter.ai/blog/robot-data-annotation | 2026-10-04 | snippet |
| 15 | Traceplane, We Audited 10 Popular Open-Source Robot Datasets | https://traceplane.ai/blog/we-audited-10-robotics-datasets | 2026-10-04 | fetched |
| 16 | BERTopic docs, Clustering | https://maartengr.github.io/BERTopic/getting_started/clustering/clustering.html | 2026-10-04 | snippet |
| 17 | Zhu et al., Gibbs-BERTopic (IEEE 2025) | https://ieeexplore.ieee.org/iel8/6287639/10820123/10930480.pdf | 2026-10-04 | snippet |
| 18 | Preiss, Arbeit & Berghammer, Evaluation of Text Cluster Naming with Generative LLMs, JDS 22(3) 2024 | https://jds-online.org/journal/JDS/article/1385 | 2026-10-04 | fetched |
| 19 | Snorkel, Essential Guide to Weak Supervision | https://snorkel.ai/data-centric-ai/weak-supervision/ | 2026-10-04 | snippet |
| 20 | Label Studio / HumanSignal, Active Learning guide | https://docs.humansignal.com/guide/active_learning | 2026-10-04 | snippet |
| 21 | AWS SageMaker Ground Truth, Automated Labeling | https://docs.aws.amazon.com/sagemaker/latest/dg/sms-automated-labeling.html | 2026-10-04 | snippet |
| 22 | Wani et al., Comprehensive analysis of clustering algorithms (2024, review) | https://pmc.ncbi.nlm.nih.gov/articles/PMC11419652/ | 2026-10-04 | snippet |
| 23 | sklearn metrics (ARI) — evaluation vocabulary | https://scikit-learn.org/stable/modules/generated/sklearn.metrics.adjusted_rand_score.html | 2026-10-04 | snippet |
| 24 | Open X-Embodiment paper (527 skills / 160,266 tasks) | https://arxiv.org/abs/2310.08864 | 2026-10-04 | snippet |
| 25 | EmergentMind, Open X-Embodiment Dataset overview (canonical skill categories) | https://www.emergentmind.com/topics/open-x-embodiment-dataset | 2026-10-04 | snippet |

Internal evidence: `experiments/clustering/results/README.md` (EXP-2.5-01…08 + correction),
`agents/decisions/0026-cluster-proposals.md`, `agents/decisions/0020-deterministic-monitoring-notifier.md`,
`agents/decisions/0014` (dependency-free core).
