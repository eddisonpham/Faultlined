# ADR 0033: Build coverage — what a build contains, and what it lacks

- **Date:** 2026-10-07
- **Status:** accepted
- **Stage:** post-stage-5, from the [curation improvement programme](../research/2026-10-07-curation-idea-improvements.md)
  (idea C2), from the question [EXP-0020](../experiments/0020-curation-ab-end-to-end.md) could not answer;
  evidence in [round-2 research](../research/2026-10-07-dataset-coverage-and-diversity.md)
- **Related:** [ADR 0032](0032-behavioural-fingerprints.md) (the redundancy report this complements),
  [ADR 0029](0029-task-vocabulary-first.md) (the vocabulary the task gaps are drawn from),
  [ADR 0018](0018-episode-quality-signals.md)/[ADR 0023](0023-quality-metrics-honesty.md) (the verdict),
  [ADR 0007](0007-lineage-and-run-records.md) (builds and manifests), [ADR 0020](0020-deterministic-monitoring-notifier.md)
  (no model in a decision path), [ADR 0014](0014-minimal-ui-server-rendered.md) (stdlib UI)

## Context

[EXP-0020](../experiments/0020-curation-ab-end-to-end.md) tested the redundancy report's claim end to
end and it held: a build of the report's representatives exported **25 % of the control's bytes for
100 % of the planted behaviours**. It also made a second thing obvious. The treatment build is
*narrower* than the control, and **nothing in the product can say in what way** — whether the
smaller build still holds every task the operator cares about, whether it dropped an embodiment, or
whether it is all `smooth` episodes and no `jerky` ones. A build's manifest lists its members; the
build page draws its lineage; both answer "which episodes", and neither answers "which behaviours".

What the industry does with this question is measure it *once, offline, in a paper*:

- **AgiBot / Shanghai Innovation Institute** (arXiv:2507.06219) decomposes diversity into **task
  (what to do), embodiment (which robot) and expert (who demonstrates)**, and finds task diversity
  outweighs demonstrations-per-task while expert diversity *confounds* learning. That is a study: its
  axis decomposition is the contribution, and reusing it on another dataset means writing another
  study.
- **Open X-Embodiment** (arXiv:2310.08864) aggregates published per-dataset statistics into a
  diversity analysis; **DROID** (arXiv:2403.12945) achieves diversity by construction and publishes
  counts. Neither ships a per-mixture report a user can consult.
- **Dataset Cartography** (Swayamdipta et al., EMNLP 2020) needs a model trained several times over
  the data — the dependency ADR 0020 rules out.
- **Croissant / dataset cards / Datasheets** are the closest to a product surface, and they are a
  *claim* the publisher fills in, not a measurement computed over a mixture. A card can say
  "diverse" and be wrong.

The observation this ADR rests on is the same one ADR 0032 rested on: **the axes are already
persisted.** Task is `episodes.metadata->>'task'` normalised against a human-approved vocabulary
(ADR 0029); embodiment is `episodes.metadata->>'robot'` (with the readers' `robot_type` fallback that
`builds/export.py` already honours); format is `episodes.format`; quality is
`episode_quality.verdict`. The three axes a study spent its budget to separate are, here, columns on
the episode row. What is missing is the aggregation and the surface.

## Decision

**1. Coverage is a property of a build, computed on read. Nothing is stored.**
No coverage table, no materialised rollup, no cached aggregate to invalidate. The precedent is
ADR 0029's unmapped queue: the derived thing is a *query* over facts that already exist, so it cannot
drift from them. A stored rollup would have to be invalidated on every ingest, vocabulary edit and
build, and would be wrong in the window between.

**2. The axes are the attributes the catalog already persists, and nothing else.**
`task`, `robot`, `format`, `verdict`. No new column, no new field on the episode, no derived
descriptor. A value that is absent is reported as absent under a named label (`(no task)`,
`(no robot)`, `(unscored)`) rather than dropped, because "we do not know" and "we have none" are
different facts and a coverage report that hides the first is the failure ADR 0023 exists to prevent
one layer down.

**3. Two scopes, and the gap is the difference.**
Every axis is reported for the **build** and for the **catalog**, and the gaps are the catalog's
values the build does not hold. A report computed only over a build's own members can describe the
build and cannot see what is missing from it; the reference set is what makes the report actionable.

**4. Task gaps are drawn from the vocabulary, not from the catalog.**
A `task_vocabulary_entries` row with no episode in the build is a gap even when it has no episode
anywhere. This is the case that changes a decision — *"you named `fold the cloth` and never recorded
it"* — and it is only expressible because ADR 0029 made the vocabulary a first-class table. It also
bounds the gap list by the operator's own vocabulary rather than by the catalog.

**5. Bounded by construction.**
Per axis: at most `VALUE_LIMIT = 20` values in the distribution and `GAP_LIMIT = 20` gaps, each with
an explicit truncation flag and the true total. The payload is bounded by axes × limits, **not by the
episode count**. This is a rule, not a preference: `quality/summary` already ships 2.2 MiB at 10 k
episodes and `/ui/insights` 5.2 MiB ([EXP-0015](../experiments/0015-feature-latency-at-three-scales.md),
audit item #8), and a coverage surface that grouped by anything episode-shaped would be a new
instance of the problem this programme is meant to help fix.

**6. Ordering is deterministic and meaningful.**
Values by count descending then value ascending, so the distribution reads most-common-first and two
runs over one catalog produce identical bytes. Gaps alphabetically. The ordering is part of the
report, not an accident of the query planner.

**7. It advises; it does not gate.**
No coverage figure enters a build's identity, no build is refused for a gap, and the report says it
is read-only. Like ADR 0032, this proposes and does not decide: a build missing a task is a fact
about the build, and whether that matters is the operator's call.

**8. One report, two surfaces.**
`GET /api/v1/builds/{hash}/coverage` (404 for an unknown build) and a section on `/ui/builds/{hash}`,
built by one shared helper so the route and the page cannot disagree — the same construction ADR 0032
used for the redundancy report.

**9. The AI-format surfaces are untouched.**
The report is not part of the LeRobot v3 export (ADR 0025) and no export behaviour changes. An
export that silently dropped or added episodes based on coverage would make the build's identity and
its contents disagree.

## Considered and not decided here

- **A dimension-profile signature axis** (which judged dimensions an episode has, so "this build has
  no episode with a gripper dimension" becomes visible). Deferred, not rejected: it needs a canonical
  signature function that `analysis/fingerprint.py` and this report both read the dimension layout
  through — one definition, not two — and that belongs with the fingerprint work rather than here.
- **Cross-axis cells** (task × robot, and the empty cells between them). Deferred for the same
  reason C4 exists: a two-axis cross-product is a much larger payload and needs a bound of its own.
- **A second task gap list drawn from the catalog's raw task strings.** Decision 4 makes the task
  axis's gaps the *vocabulary's*, which is what lets an unfilled label be a gap — and it has a
  consequence that the end-to-end drill exposed: an operator with an empty vocabulary reads
  `0 of 0 tasks absent` even when the catalog holds tasks this build left out
  ([EXP-0021](../experiments/0021-build-coverage-cost-and-what-it-sees.md)). The alternative is two
  lists — the operator's named gaps beside the catalog's dropped values — which is more faithful and
  a larger payload. Deferred, not rejected: the *values* column already names every task the build
  holds, so the missing half is visible by comparison, and a second list should be added when an
  operator is found curating with an empty vocabulary rather than on the strength of an argument.
- **Diversity indices** (entropy, Simpson, effective-number-of-tasks). Rejected for now: a single
  number cannot be acted on, which is the whole argument of decision 3. Revisit only if the gap list
  turns out to be too long to read, which the measurement will show.
- **A "coverage gate" on builds.** Rejected: decision 7. A build is an immutable, content-addressed
  artefact; refusing to create one because it is narrow would make the hash a function of a policy
  opinion rather than of the data.

## Consequences

- One pure module (`analysis/coverage.py`), one aggregate query, one route, one UI section, and a
  measurement script — the same shape as ADR 0032, deliberately.
- The report is a **second, independent statement about a build** alongside its manifest, and the two
  are computed from different sources (the manifest from the builder, coverage from the catalog), so
  a disagreement between them is a real signal rather than a restatement.
- Cost is paid per request on the catalog side, and it was measured
  ([EXP-0021](../experiments/0021-build-coverage-cost-and-what-it-sees.md)): **31 rows and 2.2 KiB at
  100, 1 000 and 10 000 episodes** (2 221 → 2 296 B, the growth being the digits of the counts),
  **126.0 ms read plus fold at 10 000** inside the 200 ms target, against `quality/summary`'s 2.2 MiB
  at the same scale. Two of the three numbers that make up that 126 ms are connection setup, not the
  aggregate (decision-level consequence recorded as B-022, not fixed here).
- **The falsifier, stated before running, and its result:** an empty gap list would have dropped the
  idea rather than produced a polish task; a payload that grew with the episode count, or a read over
  200 ms at 10 k, would have meant the design was wrong. All three clauses are **MET**, and the gap
  list is real — a 100-episode build of a 10 000-episode catalog named 20 of 112 gaps with the true
  total beside them, and a build of the whole catalog still names the 12 tasks the operator declared
  and never recorded.
- On the curation programme, the report did what it was built for:
  [EXP-0021](../experiments/0021-build-coverage-cost-and-what-it-sees.md) shows the compacted build
  holds **the same 28 distinct axis values** as the control at 25 % of the episodes — a second, and
  independent, statement of EXP-0020's claim. It also shows plainly that coverage is a **guard on a
  curation step rather than the detector of its saving**: the two arms' per-axis distributions are
  identical (4.2 % per task either way), because seeing *within*-task redundancy is ADR 0032's job.
