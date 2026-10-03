# ADR 0027: Triage uncertain task strings and organize the operator surface around work

- **Status:** accepted
- **Date (UTC):** 2026-10-01
- **Deciders:** owner + implementer agent
- **Related:** [ADR 0014](0014-minimal-ui-server-rendered.md), [ADR 0021](0021-frontend-instrument-pass.md), [ADR 0024](0024-observability-visual-surface.md), [ADR 0026](0026-cluster-proposals.md), [frontend design](../architecture/frontend-design.md)

## Context

ADR 0026 intentionally stopped at confirm/release for whole proposals. A human could not resolve the ambiguous residue: a singleton may be a genuinely new robot-task class or a fragment of an existing class, and a core accepted near the online-centroid radius may be an unstable boundary assignment. The current page also draws a word-hashed point projection with axes that have no meaning, while its fixed intrinsic SVG width leaves visual weight dependent on the containing card rather than the available width. The page shows some counts but makes a reader infer relative class size from a tiny treemap.

The operator UI has grown to twelve destinations. A flat row of twelve links is not a scalable information architecture. No evidence says two pages should be merged, so grouping by work is preferable to hiding or combining their distinct tasks.

Design research (2026-10-01) supports a task-first hierarchy, count/encoding agreement, simple labeled forms, and a review-to-curation workflow: NN/G visual hierarchy and progressive disclosure; W3C WAI labels/forms guidance; Foxglove public search/curation workflow. See the research notes and exact URLs in `architecture/frontend-design.md`.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Explicit bounded uncertainty queue + separate human labels (chosen)** | Exposes reviewable cases; no probability fiction; human decisions survive rebuilds separately | Adds persistence and a small human-review lifecycle |
| Automatically confirm low-distance assignments | Less operator work | The confidence signal is uncalibrated; it would turn algorithmic proximity into false authority |
| Only retain current whole-cluster confirm/release | No new schema | Leaves the exact uncertain samples the user asked about without a useful action |
| Keep projected point cloud | Shows membership topology at a glance | Axes have no meaning, layout suggests geometric certainty, and this task-string data lacks a useful spatial interpretation |
| Treemap alone | Compact area encoding | Small cells make exact comparison and labels difficult; prior fixed SVG width did not fill the card |
| **Fluid ranked bars with printed counts and shares (chosen)** | Direct comparison; full width; counts let people audit the scale | Long tail is ranked/scrollable rather than all tiny classes competing at once |
| Merge nav destinations | Potentially fewer top-level links | No usage evidence; distinct questions could become hidden in overloaded pages |
| **Group nav by work, keep pages distinct (chosen)** | Scales and preserves destinations | Adds one interaction level on narrow screens |

## Decision

1. The review queue uses two explainable signals: (a) a singleton with no cluster neighbors and (b) an existing assignment whose cosine distance is near the stored run radius. The queue says “no supporting neighbors” or shows distance/radius; it never displays an uncalibrated confidence percentage.
2. A reviewer may select bounded task strings, create a named human class, or dismiss selected strings as not a useful class. Human review state is persisted separately from `cluster_confirmations`: it neither edits the task metadata nor pretends to change the automatic proposal partition. Decisions remain revisable; dismissals can be undone.
3. Assignment uncertainty must be measured against each point's *final* proposal centroid and the exact radius/config used for the stored run, not a guessed score from sentence-vector layout. Review order is deterministic and list reads are bounded.
4. Replace both cluster figures with one responsive ranked size visualization. Length encodes episode count; each row states episode count, distinct task-string count, and share of eligible episodes. A count table/list remains the accessible source of the same values. “Largest class” describes a proposal until a human confirms it.
5. Recompose the page around operator questions: queue and scale first, then review and confirm actions, then rebuild controls/history. Forms remain server-rendered and usable without JavaScript.
6. Group navigation by intent (curate data, operate engine, inspect catalog); keep existing destinations distinct and visible without relying on icons or color alone. Retain the compact instrument wordmark and robot-data-engine identity while reducing the flat-row density.
7. No dependency, frontend framework, model, architecture boundary, or third-party service is added. HTML/SVG remains server rendered; Postgres remains system of record.

## Consequences

- Adds bounded human review tables/repository/API/UI forms and a forward-only schema extension. The new endpoints are additive and require OpenAPI regeneration.
- Final-centroid distance is a transparent review heuristic, not an independently calibrated confidence estimate. This limitation is shown in the UI and experiment notes.
- Grouped navigation adds a click to less-frequent pages; keyboard and no-JS access must still be verified in-browser.
- The size visualization is useful for prioritizing volume, not determining semantic correctness; the task examples and human-review step remain essential.

## Docs updated

- `agents/architecture/frontend-design.md`
- `agents/architecture/frontend.md`
- `agents/spec/definition-of-done.md`
- `agents/HANDOFF.md`
- `agents/experiments/` record and registry
