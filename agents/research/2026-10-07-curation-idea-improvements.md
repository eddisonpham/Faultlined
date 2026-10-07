# Improving the industry's curation ideas for Faultlined (and which one to build)

**Date:** 2026-10-07 · Derived from [the platform landscape](2026-10-07-platform-landscape-and-curation.md),
read against the tree at `b22f6e1` and the open items in the
[program audit](../reviews/2026-10-06-program-audit/improvement-ranking.md) and
[implementation status](../implementation/status.md).

**Method.** Each aggregated industry idea is restated as a *claim about this repository* ("we do not
have X"), paired with the constraint that makes the industry implementation inapplicable here, then
rewritten so it survives those constraints. The rewrite is only accepted if it is **measurable** —
every candidate below states the number that would decide it, and the falsifier that would kill it.
Machine-checkable claims about the current tree are marked with the file they were verified in.

---

## 1. What the current tree already answers (do not rebuild)

Verified by reading the code, not the docs:

| Claim | Evidence |
|---|---|
| Exact re-ingest is already deduplicated | `episodes` has `UNIQUE (source_hash, episode_key)` — `catalog/database.py:47`; failure mode F26 |
| Every episode already carries a per-dimension motion character, scale-free | `episode_quality.dims` → `DimQuality.to_dict()` with `norm_delta_std` and `mean_abs_delta_norm`, both normalised by the dim's own range — `analysis/quality.py`, `catalog/repository.py::record_episode_quality` |
| Every episode already carries a bounded motion *shape*: up to 240 `(seconds, score)` points, split into runs at recording gaps | `EpisodeQuality.motion_trace`, `analysis/quality.py::_trace` + `_thin(filled, TRACE_POINTS)` |
| Every episode already carries temporal character | `stall_ratio`, `gap_ratio`, `max_gap_seconds`, `integrity` |
| Task strings are already normalised to a human-approved vocabulary, with a derived unmapped queue | `catalog/vocabulary.py` (ADR 0029) |
| A build's membership is already ordered and reproducible | `build_episodes ... ORDER BY ordinal`, identity = SHA-256 of manifest alone (`builds/service.py:41`) |

The consequence is the thesis of this document: **the descriptor a dedup/coverage feature needs is
already in the catalog.** It is not computed by a model, it is computed by `analyze()` at ingest, and
it is already persisted. Nothing has to be re-read from an artifact blob, no frames have to be
touched, and no dependency has to be added. What is missing is the code that *compares* two of these
descriptors and the surface that reports the result.

---

## 2. The improved idea set, ranked

Ranking rule (same as the program audit): leverage = (impact × user value × engineering value) /
(difficulty × risk), with an extra term specific to this exercise — **does it survive the
constraints?** (no model, no new dependency, no network, one host, stdlib UI).

### C1 — Behavioural fingerprints: redundancy, uniqueness and similarity from what ingest already computed

**Industry version.** SemDeDup (#I1): embed, k-means, drop pairs above a cosine threshold.
FiftyOne Brain (#I2): near-duplicates, uniqueness, representativeness — all over an embedding space.

**Why it does not transfer.** An embedding of an *image* is not available here: we never open the
frames, and the engine has no numerical library by design. Building it would mean adding torch or
numpy to the runtime dependency set, a GPU pass over the whole catalog, and a learned threshold —
for a local-first, single-operator tool. ADR 0020 rejected exactly this shape of argument for the
monitoring detector ("a non-reproducible output for a system whose value is being trusted").

**The improvement.** Build the same three measures over a **fingerprint assembled from
already-persisted, already-scale-free signals**, and keep it a *pure function with no I/O*:

| Component | Source (already stored) | Why it is comparable across episodes |
|---|---|---|
| **shape** | `motion_trace` resampled to a fixed 32 points on a normalised `[0,1]` time axis, divided by its own mean | Scale-free (the trace is already normalised per dim by range) and duration-free — a 6 s demo and a 60 s demo of the same motion land on the same curve |
| **dynamics** | per-dim `norm_delta_std` and `mean_abs_delta_norm` from `dims`, keyed by the dimension's **own field name** (`analysis/quality.py::_field`, the ADR 0018 amendment) | Both are divided by the dim's own range, so units and robot scale cancel |
| **temporal** | `stall_ratio`, `gap_ratio` | Fractions, not durations |

Distance = a weighted mean over the components *that both episodes have*, renormalised by the weights
present, so an episode with no clock (no trace) still compares on dynamics and temporal character and
**says so** rather than silently scoring 0. Clustering is SemDeDup's own greedy shape — walk the
episodes in a deterministic quality order, keep the first, and attach anything within `threshold` of a
kept episode to it — with a human-readable rank instead of a model score, and the *reason* (which
component was close, by how much) attached so an operator can disagree with it.

**What it buys, in the industry's own vocabulary:** near-duplicates (C1a), uniqueness /
representativeness (C1b — the same distance, read the other way: how far is this episode from its
nearest neighbour), and "find episodes like this one" (C1c — the API endpoint FiftyOne's similarity
search is).

**Cost:** one pure module, one bounded catalog query, zero new dependencies.

**Decision metric:** on a corpus with **planted** near-duplicates, precision and recall against the
planting; on real fixtures, whatever it actually finds, reported as-is. Plus report latency at 100 /
1 000 / 10 000 episodes, and the payload bound.
**Falsifier:** if the fingerprint cannot separate planted duplicates from planted non-duplicates at
any threshold (e.g. every threshold has precision below the base duplication rate, or recall is ~0 at
precision > 0.5), the measure is not carrying information and the idea dies. Stated before running.

### C2 — Build coverage: what the build contains, and what it does not

**Industry version.** OXE's diversity analysis and mixture design (#I3); DROID's curation-by-design.
Both are *manual* studies — a figure in a paper, not a product feature.

**Why it does not transfer as-is.** There is nothing to transfer: the industry answer is "an author
writes an analysis script". The idea is real, the implementation does not exist to copy.

**The improvement.** Make coverage a **first-class property of a build**, computed from the catalog
rather than from a notebook: for each axis the catalog already knows (task-vocabulary entry, robot,
format, quality verdict, dimension-profile signature), report the build's covered values, the
catalog's covered values, and the **specific gaps** — "this build has 0 episodes for `fold the
cloth`", "3 of 9 robots are absent". Deterministic, order-independent, and bounded by the axes rather
than by the episode count.

**Relation to C1.** C2 needs C1's dimension-profile signature to be interesting (otherwise "robot"
and "task" are the only axes, and the vocabulary page already shows those). C1 does not need C2.
That ordering decides the sequence.

**Decision metric:** coverage report latency and payload at 10k episodes (the audit's item #8 says
unbounded aggregates are already the top performance problem — 2.2 MiB and 5.2 MiB payloads at 10k —
so C2 must be bounded by construction or it makes that worse).
**Falsifier:** if the gap list on the real fixtures is empty or trivially "everything is covered",
the report is a table nobody reads.

### C3 — A decision record for curation (why an episode is not in a build)

**Industry version.** FiftyOne's leaky-splits analysis; the data-engine feedback loop (#I4/#I5).
**Improvement.** Extend the existing slice-impact view — which already attributes every drop —
into a build-level "rejected because" report, and make it part of the build manifest's explanation.
**Sequence:** after C1 and C2 — a rejection reason is only useful once there are filters worth
rejecting for (near-duplicate being the new one).
**Cost:** mostly UI. **Depends on:** C1 producing something to reject.

### C4 — Unbounded aggregates (audit item #8), with the rollup question settled by measurement

Already on the roadmap ([proposed-v2](../reviews/2026-10-06-program-audit/proposed-v2.md) P2 item 8).
Listed here only to state the rule that constrains C1/C2: **any new aggregate surface is bounded by
construction** (a capped episode set, a bounded axis list, a stated truncation), so this programme
does not add to the problem it is supposed to help fix.

### C5 — Orchestration / workflow graph (OSMO) and container formats (Robo-DM, Lance)

**Rejected for this programme**, with the reason recorded rather than left implicit: OSMO solves a
heterogeneous-compute problem this repository does not have (one host, ADR 0012), and a container
change would have to beat LeRobot v3 in downstream accuracy *and* in ecosystem reach, which is a
multi-week experiment with no requirement behind it. Both belong in
[technology-matrix.md](technology-matrix.md) as **excluded, with trigger** rather than in a build
plan.

### C6 — Event-driven capture, retention tiering (Heex, ReductStore)

**Rejected:** capture already happened and is out of scope (the engine ingests files that exist), and
Faultlined stores catalog rows, not the fleet's data lake. Recorded in the matrix, not built.

---

## 3. The chosen slice: C1, measured

**The claim to test:** the fingerprint is informative — it separates episodes that are the same
behaviour recorded twice from episodes that are not — and it is cheap — a bounded report over
thousands of episodes in a few hundred milliseconds, with no new dependency and no artifact reads.

**Sequence for this iteration.**

1. `src/data_engine/analysis/fingerprint.py` — pure: build a fingerprint from a quality row, measure
   the distance between two, and fold a set into a redundancy report (groups, representative,
   reason).
2. `PostgresCatalog.episode_fingerprint_inputs(ids)` — one bounded query, one round trip.
3. `GET /api/v1/builds/{hash}/redundancy` — the report for a build, 404 for an unknown build,
   bounded episode set with explicit truncation.
4. `/ui/builds/{hash}` renders it: distinct behaviours, redundant count, the groups with links.
5. A measurement script that seeds a catalog with **planted** duplicates and reports precision,
   recall and latency at three sizes — the thing that decides whether this was worth building.

**Explicitly deferred to the next iteration of the loop** (so this one stays the smallest useful
slice): the uniqueness column on `/ui/episodes`, `GET /api/v1/episodes/{id}/similar`, C2 coverage,
and wiring "redundant" into slices as a filter. Each is a small addition on the same primitive, and
each is a candidate for the next round of *implement → review → black-box test → reflect*.

**The improvement over the industry idea, stated plainly.** SemDeDup and FiftyOne both need a model
pass before curation is possible. Here, curation is a *pure function of what ingest already wrote*, so
it is deterministic, explainable (an operator can read which component was close), free at the
dependency level, and available the moment an episode exists rather than after a batch job. That is
the sense in which this is an improvement on the aggregated idea rather than a copy of it — the
measure is coarser than a CLIP embedding, and the trade is deliberate: it trades a little detection
power for determinism, zero dependencies and no model in any decision path.
