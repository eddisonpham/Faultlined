# ADR 0032: Behavioural fingerprints — redundancy and similarity from signals ingest already computed

- **Date:** 2026-10-07
- **Status:** accepted
- **Stage:** post-stage-5, from the [curation improvement programme](../research/2026-10-07-curation-idea-improvements.md)
  (idea C1); landscape and sources in
  [the platform research](../research/2026-10-07-platform-landscape-and-curation.md)
- **Related:** [ADR 0018](0018-episode-quality-signals.md) (the quality summary, and the amendment
  that made a dimension's *field* name decide its role), [ADR 0023](0023-quality-metrics-honesty.md)
  (refuse what cannot be measured), [ADR 0020](0020-deterministic-monitoring-notifier.md) (no model
  in a decision path), [ADR 0014](0014-minimal-ui-server-rendered.md) (stdlib UI), [ADR 0012](0012-host-based-development.md)
  (one host), [ADR 0007](0007-lineage-and-run-records.md) (builds, lineage)

## Context

The engine has no answer to "are two of my episodes the same behaviour recorded twice?". Exact
duplicates are handled at the identity level — `episodes` carries `UNIQUE (source_hash, episode_key)`,
so re-ingesting a file is a no-op (failure mode F26) — and textual duplicates are handled by the task
vocabulary ([ADR 0029](0029-task-vocabulary-first.md)), which normalises task *strings*. Neither
notices a re-teleop, a second take of the same demo, or the same motion recorded by two runs that
produced different bytes.

Every comparable platform answers this, and every one of them answers it with an embedding:

- **SemDeDup** ([arXiv:2303.09540](https://arxiv.org/html/2303.09540v3)) embeds each item with a
  pretrained model, k-means-clusters, and removes pairs above a cosine-similarity threshold, keeping
  one; productised as a stage in
  [NVIDIA NeMo Curator](https://docs.nvidia.com/nemo-framework/user-guide/25.07/datacuration/semdedup.html).
- **FiftyOne Brain** ([docs](https://docs.voxel51.com/brain/)) exposes near-duplicates, uniqueness,
  representativeness, leaky splits and hardness, all computed over an embedding space, and describes
  itself as turning curation "from an art into a measurable science".

That route is closed here, by decision rather than by accident: the engine's runtime dependency set is
eight packages with **no numerical library at all** (`pyproject.toml`; `numpy`/`torch`/`model2vec`
live behind the `experiments` extra and nothing in `data_engine` imports them), frames are never
opened — a LeRobot artifact is the Parquet file, an MCAP artifact is the bag — and ADR 0020 rejected
a learned detector for the monitoring path on the grounds that a non-reproducible output is the wrong
property for a system whose value is that it can be trusted.

But the observation this ADR rests on is that **the descriptor is already in the catalog**:

| Signal | Where it is already written | Why it is comparable across episodes |
|---|---|---|
| Motion shape | `episode_quality.motion_trace` — up to 240 `(seconds, score)` points in runs split at recording gaps, `analysis/quality.py::_trace`/`_thin` | The score is already normalised per dimension by that dimension's range |
| Per-dimension dynamics | `episode_quality.dims` — `norm_delta_std`, `mean_abs_delta_norm` per dimension | Both divided by the dimension's own range, and classified by the dimension's own field name (ADR 0018 amendment) |
| Temporal character | `stall_ratio`, `gap_ratio`, `maximum gap` | Fractions and a bounded duration |
| Task, robot, format | `episodes.metadata` | Already the basis of the vocabulary and the catalog views |

All of it was computed at ingest by `analyze()` (ADR 0018) and persisted (`record_episode_quality`).
None of it requires reading an artifact, and none of it requires a model. What was missing was only
the code that compares two of them.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **A. Nothing; exact dedup is enough** | Zero cost; F26 already covers the literal case | Leaves the highest-value curation gap from the research open; a re-teleop stays in every build |
| **B. Embedding-based semantic dedup (SemDeDup as published)** | Industry-standard; strongest detection power; a real threshold to tune | Adds torch/numpy to the runtime set; needs a GPU pass over the catalog; a learned, unexplainable verdict; contradicts ADR 0020's reasoning and the dependency policy in CLAUDE.md; frames are not in memory anyway |
| **C. Perceptual hash of the artifact bytes** | Cheap, exact, no model | Only catches re-encodes of identical bytes; says nothing about two different recordings of the same motion; needs an artifact read per episode |
| **D. Deterministic fingerprint over already-persisted signals (chosen)** | No new dependency; no artifact read; deterministic and explainable (the report names which component was close and by how much); available the moment an episode exists; survives every existing constraint | Coarser than an embedding; will miss duplicates whose motion differs but whose meaning does not; needs a measured threshold rather than an established one |

## Decision

1. **A fingerprint is a pure function of a persisted quality row and an episode's metadata.**
   `analysis/fingerprint.py` builds it with no I/O, in the same style as `analysis/quality.py`:
   a frozen dataclass, stdlib only, testable without a database.

2. **Three components, and comparability is explicit.** `shape` (the motion trace resampled to a
   fixed number of points on a normalised `[0,1]` time axis, divided by its own mean),
   `dynamics` (per-dimension `norm_delta_std` and `mean_abs_delta_norm`, keyed by the dimension's
   own field name, compared only over the dimensions both episodes have), and `temporal`
   (`stall_ratio`, `gap_ratio`). The distance is a **weighted mean over the components both episodes
   actually have**, renormalised by the weights present — never a sum with a missing component
   silently scored zero. An episode ingested without a clock has no trace and still compares on the
   other two; the report says which components were compared.

3. **Clustering is SemDeDup's greedy shape with an explainable rank.** Episodes are walked in a
   deterministic quality order (verdict, then lower stall ratio, then more judged dimensions, then
   id — "keep the best take"), and an episode within `threshold` of an already-kept episode joins
   that group. The group carries the distance and the component that dominated it, so a human can
   read the reason and disagree.

4. **It proposes; it never removes.** The report is read-only. Nothing is deleted, no episode is
   quarantined, and **the fingerprint does not enter a build's identity** — a build's hash is the
   SHA-256 of its manifest alone (ADR 0007, NFR-004), and adding a derived signal to it would
   silently change the hash of every existing build. Curation acts on the report through the existing
   mechanisms (a slice, a build selection), so the decision stays with the operator and stays
   auditable.

5. **The threshold is a measured parameter, not taste.** The module ships a default constant with a
   documented calibration: the redundancy report's precision and recall are measured on a corpus with
   *planted* near-duplicates, and the default is the value the measurement supports. Until that
   experiment is recorded, the default is described as provisional in the code and in the docs.

6. **Every surface is bounded by construction.** The catalog read takes an explicit episode set
   (or a build's membership, itself bounded); the report states when it truncated. This is the rule
   the [curation programme](../research/2026-10-07-curation-idea-improvements.md) §2 C4 imposes on
   itself, because unbounded aggregates are already the audit's top performance finding
   (2.2 MiB and 5.2 MiB payloads at 10k episodes).

7. **Where it lives.** Pure logic in `analysis/`, one bounded query on `PostgresCatalog`, one
   `GET /api/v1/builds/{hash}/redundancy` route with a Pydantic response model, and one section on
   the existing `/ui/builds/{hash}` page. No new page and no new navigation entry: the redundancy of
   a build is a property of the page that already shows the build.

## Consequences

**Positive.** The curation layer the research identifies as the industry's current investment becomes
a property of ingest rather than a batch job: deterministic, dependency-free, explainable, and
available on a single host with no model. It composes with everything already built — a near-duplicate
group is episode ids, so it can become a slice, be excluded from a build, or be linked from the
lineage page without new machinery, and the reason string makes the result reviewable rather than
oracular.

**Negative, and stated.** The measure is coarser than an embedding and will miss semantic duplicates
whose motion differs; the threshold is calibrated on planted data, so its precision on real
teleoperation is claimed only as measured. The report's greedy clustering is order-dependent by
design (the order is the deterministic quality rank), so it is a partition of the set, not a
similarity graph — two episodes both similar to a third are not grouped with each other.

**What becomes harder / must be updated.** The fingerprint is a new consumer of `episode_quality`;
if the quality summary ever changes shape (a fourth component, a different trace cap) the fingerprints
change with it and the threshold's calibration is invalidated. That coupling is recorded here rather
than discovered later. `agents/architecture/components.md`, `api.md` and the observability of the new
route also need updating in the same commit.

## Docs updated

- `agents/architecture/components.md`, `agents/architecture/api.md` (new route and module)
- `agents/implementation/status.md`, `agents/HANDOFF.md`
- `agents/benchmarking/backlog.md` (the calibration measurement), `agents/experiments/` (the record)
- `agents/research/technology-matrix.md` (embedding-based dedup recorded as excluded, with trigger)
- `agents/decisions/README.md` (this ADR)
