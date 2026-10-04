# ADR 0029: The task vocabulary is the source of truth; clustering only proposes

- **Date:** 2026-10-04
- **Status:** accepted
- **Stage:** post-stage-5 operator-convenience work
  ([implementation plan](../implementation/vocabulary-and-operator-gaps-plan.md) Track A)
- **Related:** [ADR 0026](0026-cluster-proposals.md) (superseded *in part* — see below),
  [ADR 0027](0027-cluster-review-and-ui-information-architecture.md) (triage surface, absorbed),
  [ADR 0014](0014-minimal-ui-server-rendered.md), [ADR 0020](0020-deterministic-monitoring-notifier.md),
  [ADR 0028](0028-versioned-catalog-migrations.md),
  [clustering-methods-verdict.md](../research/clustering-methods-verdict.md) (the evidence)

## Context

The cluster proposals surface (ADR 0026) groups task strings into proposals a human names.
The measurements behind it hold up — extraction before grouping is the difference between
533 fragments and 47 coherent groups (EXP-2.5-01…08) — but the *product shape* fails at the
point of use:

1. **Labels are typed ad hoc** against whatever fragment is on screen, so proposals carry
   nonsense names; nothing forces a label to name a durable thing.
2. **Merges are invisible** inside a proposal; an operator cannot see what was combined.
3. **The review queue is unbounded**: every rebuild re-surfaces everything, so the feature
   "requires constant human intervention" — the exact complaint it was built to end.
4. **82% lexicon coverage silently fragments** the 18% it misses, and nothing says so.
5. **Proposal identity is content-derived** (`blake2b(core)`), so changing extraction options
   re-keys every proposal (ADR 0026 §3's own lesson).

The engineering literature frames this as **entity resolution with a curated vocabulary**,
not topic modeling (verdict §2.1): the robot-data industry treats task strings as vocabulary
to *edit* — LeRobot's `task_replacements`, OXE's 527 curated skill categories (verdict §2.2).
The verdict (Option D) is to make the vocabulary the product and demote clustering to one
input.

## What is superseded, and what is not

ADR 0026 is **superseded in its pipeline shape** (proposals as the primary object; grouping
quality deciding anything) and **kept in its semantics**: a confirmation is a claim about
*task strings*, not about a row, and the >50% majority-overlap rule is how a claim that no
longer holds together gets reported as orphaned rather than silently reattached. That rule
now governs vocabulary mappings across merges and splits. ADR 0027's triage surface is
absorbed: "dismiss" becomes a mapping, and the uncertain-string queue becomes the unmapped
queue.

## Decision

1. **A task vocabulary is the source of truth.** `task_vocabulary_entries` (stable id,
   human-approved `preferred_label`, `core`, notes) + `task_vocabulary_mappings`
   (task_string → entry, `provenance` = `ingest | confirm | merge | split | dismiss`) live
   beside the episode catalog. Episode rows keep their raw task string forever — the mapping
   is a view over it, so the audit trail never changes under a rename.

2. **Entry ids are content-derived at creation and never move.**
   `voc_<blake2b(preferred_label)>` makes the backfill deterministic and collapses duplicate
   labels on creation; the id names the *entry*, not the label, so a rename keeps it.

3. **Ingest maps or leaves for the queue.** An exact mapping hit needs no work; a string
   whose extracted core matches exactly one entry's core is auto-mapped
   (provenance `ingest`) — deterministic lexicon extraction only, no model (ADR 0020).
   Ambiguous or unmatched strings are *not written anywhere*: the unmapped queue is a query
   over episodes minus mappings, so it cannot drift from the data and ingest stays cheap.

4. **The unmapped queue replaces the review queue**, ranked by episode count (kill the most
   fragmentation first). A string can be dismissed (provenance `dismiss`, entry NULL) so
   noise leaves the queue permanently — the old `dismissed` disposition, without a second
   table. "Bounded" means the queue only ever contains *novelty*.

5. **Clustering is a synonym-candidate ranker.** The extract + core machinery proposes:
   "these unmapped strings share a core with entry X" or "these strings form one new entry,
   suggested label = the core". A suggestion is a queue, never an action.

6. **Labels are proposed, humans approve.** Deterministic suggestion first (majority core —
   zero dependencies, reproducible). An optional offline LLM proposer is *not built* until
   candidate precision is measured; if built it is non-authoritative and behind the same
   confirm gate (ADR 0020: no model in the decision path).

7. **Merge and split are explicit, recorded, reversible events.**
   `task_vocabulary_events` stores the operation payload (which strings moved, from where,
   the previous label); undo is a compensating action over that payload. A merged-away
   entry can therefore be restored exactly.

8. **The old cluster surface is frozen, not deleted.** `/api/v1/clusters*` keeps answering
   from its tables as a read-only archive (the v1 contract is additive-only); its UI routes
   redirect to `/ui/vocabulary`; the tables are dropped by a later forward-only migration
   after one release. Rebuilds stop deleting anything: vocabulary rows are never keyed by
   anything a rebuild computes.

9. **Coverage stays loud.** The vocabulary page leads with unmapped share (episode-weighted
   and string-weighted), orphaned mappings, and pending candidates. Falsifiers from the
   verdict §4 are the acceptance gates: unmapped share >20% after seeding from one real
   corpus means extraction is the bottleneck (lexicon work first); candidate confirm
   precision <50% means re-measure before building anything else.

## Alternatives considered

- **Tune the current pipeline only.** Rejected: the pain is naming, merge visibility, and
  queue unboundedness, which no radius/lexicon sweep touches (verdict option A).
- **Modern clustering stack (embeddings/UMAP/HDBSCAN).** Rejected: contradicted by our own
  measurements (order sensitivity ARI −0.0005), and model dependencies violate ADR 0014.
- **Remove the feature.** Rejected: the problem is real and industry-open; slices/builds
  lose their only activity-level dimension.
- **LLM in the grouping loop.** Rejected: nondeterministic grouping in a provenance path is
  the confident-and-wrong failure EXP-2.5 already charged us for.

## Consequences

- Schema: three new tables in the idempotent baseline (fresh installs need no operator step)
  plus migration `0002` (`task_vocabulary`) which backfills entries and mappings from
  `cluster_confirmations` and the review dispositions exactly once — the first real use of
  the ADR 0028 runner.
- `catalog/vocabulary.py` is the store; `clustering/` shrinks to `extract` (kept — measured)
  plus a deterministic ranker. The online centroid machinery is not in the vocabulary path.
- `/ui/clusters` redirects to `/ui/vocabulary`; the cluster API stays frozen; the cluster
  page renderers stay one release as an archive and are removed with the tables.
- Two triage surfaces existed; there is now one (the unmapped queue), and the curation nav
  groups around it.
- The plan's A4 "EXP-2.5-style measurement" and the §4 falsifier checks land as experiment
  records, not as code paths.

## Amendment 2026-10-04 — undo refuses to replay over a newer decision

Decision 7 says undo is "a compensating action over that payload". Read literally that
lets an old undo clobber a newer one: merge A→B, re-map one of A's strings to C, undo the
merge, and A's string silently jumps back to A. That is the confident-and-wrong failure
this ADR exists to avoid, wearing a UI.

`undo_event` now checks, in the same transaction that compensates, that the state the
event recorded is still the state it left (the mapping is still where and what the event
wrote; the merged-away entry is still absent; the entry still carries the new label). If
it is not, the undo is refused — 409 at the API, the reason shown in the UI — rather than
replayed. Legitimate later work is unaffected: merging A→B, mapping a *new* string to B,
then undoing the merge restores A and leaves the new string on B.

Two supporting fixes the same work exposed, both consequences of read-then-write across
transactions:

- `map_task`/`dismiss_task` read the previous mapping outside the writing transaction, so
  under a race both writers recorded `previous: None` and the loser's undo deleted the
  string. Writers now serialize per task string on a transaction-scoped
  `pg_advisory_xact_lock` (keys taken in sorted order, so multi-string candidates cannot
  deadlock) and read the previous mapping under that lock, inside the write.
- Two concurrent renames onto one label could both pass the clash check and have the
  unique index raise `UniqueViolation`, which surfaced as a 500. That is now caught and
  answered as `LabelConflict` → 409.

Pinned by `TestConcurrentWriters` and `TestStaleUndo` in
`tests/integration/test_vocabulary.py` (real threads, real connections, real PostgreSQL).

## Docs updated

- [decisions README](README.md), [api.md](../architecture/api.md),
  [repo-layout.md](../architecture/repo-layout.md) (in the implementation slices),
  [implementation plan](../implementation/vocabulary-and-operator-gaps-plan.md) execution record.
