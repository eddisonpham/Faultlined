# ADR 0026: Cluster task strings into proposals a human confirms

- **Date:** 2026-10-01
- **Status:** accepted
- **Stage:** 4 (client-usable iteration)
- **Related:** [ADR 0024](0024-observability-visual-surface.md) (the operator UI is a first-class
  surface, not a debug view), [ADR 0025](0025-lerobot-v3-export-of-builds.md) (the client-usable
  goal of stage 4), EXP-2.5-01…08 in
  [experiments/clustering/results/README.md](../../experiments/clustering/results/README.md),
  [HANDOFF §7](../HANDOFF.md)

## Context

Every episode carries a `task` string. At 1000+ episodes a catalog holds dozens of distinct
sentences that mean the same handful of activities — `pick up the red mug`,
`pick up the blue mug` and `grab the mug` are one activity described three ways. Nothing in
the engine groups them, so a reviewer curating a slice has to read every distinct string to
know how many activities the catalog really contains.

EXP-2.5-08 ran the obvious implementation — group whole task strings by sentence
embedding — over 1200 synthetic plus 46 real LeRobot sentences and measured the result:
**533 clusters for 48 activities, 302 singletons (26%), B-cubed 0.073, fragmentation 23.4,
0 of 48 activities intact.** The scale curve got *worse* with more data. Order sensitivity
was ARI −0.0005: the order the rows arrived in decided the partition. Radius sweeping found
no setting with a good column. This was not a tuning problem, and the centroid update rule
was ruled out as the cause (a rule sweep moved the cluster count only between 531 and 607).

The same experiment then extracted the **object core** of each sentence — drop the verb
phrase, the colour and the destination, keep what is being acted on — and the same corpus
gave **47 clusters, B-cubed 0.986, zero singletons, 48 of 48 activities intact**, with the
extracted core equal to the true object 100% of the time. Extraction is the algorithm.
Embedding was proposing the wrong thing, well.

## Decision

1. **Clusters are groups of _extracted_ task strings, never embeddings of whole sentences.**
   `data_engine.clustering.extract` reduces a sentence to a core by matching the longest
   known verb phrase and removing the ignored axes; `clustering.online` then groups cores by
   cosine radius with a running-mean centroid. The measured evidence is the decision, and it
   points the other way from where the feature started.

2. **The product proposes; a human confirms.** Every group is a *proposal* with a stable key,
   a core, its members, and health numbers. A proposal is frozen only when an operator posts
   a label. Nothing in the engine treats a proposal as truth, because EXP-2.5-08 measured that
   the unconfirmed proposals were mostly wrong.

3. **A confirmation is a claim about task strings, not about a row.** `cluster_confirmations`
   stores the *task strings* a confirmation covers, with no foreign key to `task_clusters`.
   A proposal's key is a hash of its core, so changing the extraction options re-keys every
   proposal; the first implementation pointed the table at that key with `ON DELETE CASCADE`
   and one rebuild with different axes deleted every label an operator had written. On a
   rebuild, each confirmation is re-attached to whichever proposal now holds its strings.

4. **A confirmation survives only while a proposal holds _more than half_ of its strings.**
   Measured against the first rule, which matched on any overlap at all: shrink the radius
   until a confirmed pair of strings scatters into two singletons, each overlaps by one, the
   tie is broken on the key, and the operator's name lands on an arbitrary neighbour. A claim
   that no longer holds together is counted as **orphaned** and reported in the run's health
   block; it is never silently moved. The confirmation itself stays in the table.

5. **Proposals are stored, not recomputed on read.** `cluster_runs` records every rebuild
   with its options and health, so a page load cannot change the numbers it is showing and
   every configuration is comparable. A rebuild is `DELETE` + `INSERT` of the whole run in
   one transaction, so a crash leaves the previous run intact rather than a half-written
   mixture.

6. **The Clusters page is part of the operator UI, in the nav, drawn in the same
   server-rendered vocabulary as everything else.** Health tiles first, then a treemap of
   cluster sizes, a colour key, a map of boundaries, then one confirmable row per proposal,
   then the run history. No JavaScript: a control that needs scripting fails silently in
   exactly the situation an operator needs it. `/ui/clusters` is in the browser audit's page
   list, so a page added to the nav cannot go unmeasured.

7. **Reading is capped and the cap is reported.** `distinct_tasks` reads at most
   `TASK_LIMIT` (5000) rows and `health.truncated` says when it hit the cap, because a
   clustering over the first 5000 task strings is otherwise indistinguishable from a
   complete one.

8. **The engine stays installable without a model.** Tokens are hashed into a fixed
   256-dimension vector by `clustering.online.token_vector`; there is no model to download,
   no numpy in the runtime dependency set, and the cost measured on 1200 strings was 2.45 s
   on CPU (489/s).

## Consequences

- **The page can be empty and says so.** A `ProposalSet` is always truthy, so `has_run`
  looks inside it; before that existed, a catalog that had never been clustered rendered six
  healthy-looking zero statistics, which reads as a finding rather than an absence.
- **Confirming a group is cheap and reversible.** A label is one form post; releasing one
  deletes the confirmation and the proposal goes back to being a suggestion.
- **Colour is a hash of the cluster key**, so a cluster keeps its colour across rebuilds and
  the swatch in the table means the same thing in both figures. Without the key printed next
  to the treemap the colours read as decoration.
- **Extraction is a lexicon, so it is a maintenance surface.** An unrecognised verb leaves
  the core intact rather than guessing, and the health block reports the matched-verb rate
  (98% on the sample corpus, 82% on the full corpus) so a gap is visible rather than silent.
- **Proposals are not labels.** Nothing downstream consumes a proposal automatically. If a
  future slice needs activity names, it consumes confirmed clusters, and the confirmed set is
  the only part an operator vouches for.
- **Deferred:** a per-cluster detail page in the UI (members beyond the fourth are linked to
  the API), a learned extractor behind the `extra` axis set, and cross-corpus vocabulary
  bootstrapping from a larger Hub harvest.

## Alternatives considered

- **Sentence embeddings as the cluster proposer.** Rejected on measurement: B-cubed 0.073,
  0/48 activities intact, and a partition that depended on row order. It would also have
  put a model in the runtime dependency set, which ADR 0024's zero-install constraint and the
  product's local-first promise both rule out.
- **No clustering: a plain distinct-task-string list.** Honest and free, and it is what
  EXP-2.5-08's 23-clusters-for-44-strings run amounts to. It does not answer the question
  the page exists for — how many *activities* are in this catalog — which is why the
  extraction path is the default and this is the empty-axis case.
- **Persisting clusters in a vector database.** Rejected: 48-dimension float vectors over a
  few thousand strings do not need one, and it would add a service to a product whose pitch
  is `git clone` and `just run`.
- **Auto-freezing high-confidence clusters.** Rejected for now. The only confidence signal
  available is the clustering's own agreement, and EXP-2.5-08 showed the unconfirmed output
  was wrong often enough that an automatic freeze would put unearned names into the catalog.
  The health block makes the case for a threshold visible to whoever builds it.
