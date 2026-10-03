# Frontend Design and User Workflows

**Updated:** 2026-10-01. Research was recorded before the UI changes described in ADR 0027; browser results below were recorded after implementation.
**Mechanism:** server-rendered HTML + one vanilla-JS polling runtime; no client renderer or new dependency (ADRs 0014/0021).

## Who this is for, and why it exists

Faultlined serves engineers who turn robot recordings into trusted, reproducible training and evaluation data. These are engineering workflows, not a public marketing site; the primary value is reducing time spent finding bad or duplicate episodes and making every curated result inspectable and repeatable.

| Persona | Job to be done | Business value (hypothesis, not a measured claim) |
|---|---|---|
| Robotics data engineer | Ingest today's MCAP/LeRobot recordings, see what failed, repair or exclude bad material | Less manual file inspection and fewer silent data-quality regressions |
| ML / policy engineer | Find representative tasks, curate a usable slice, cite an immutable build | Less time reconciling inconsistent task strings; reproducible training inputs |
| Evaluation engineer | Freeze the exact data used for a comparison and follow lineage | A regression can be reproduced and defended rather than debated |
| Platform engineer | Notice stuck work and resource/latency problems before the queue is lost | Shorter diagnosis and less wasted compute |
| Local single-user operator | Run the same workbench on one laptop without a hosted control plane | Low setup cost, local data sovereignty, graceful offline operation |

The buyer/use-case hypothesis is robotics labs and small-to-mid-size robotics teams that own their recordings and need an auditable local data loop. This is inferred from the problem selection and public robotics data platforms, not validated with customers; the platform is not currently multi-tenant or secure for remote users.

## Operator questions and current page map

The top-level UI should answer in work order: **what happened; what needs attention; what data can I trust; what can I build from it; where did the time go?** The existing twelve destinations remain separate because they answer distinct questions and merging them now would hide functions without evidence about usage frequency.

| Page | Operator question | Primary next action |
|---|---|---|
| Status | Is the local engine and its queue alive? | Open work or submit an ingest |
| Jobs | What ran, failed, retried, or is waiting? | Inspect a job report or cancel live work |
| Episodes | Which recordings exist and what quality signals do they carry? | Inspect an episode or curate a slice |
| Failures | Why were episodes quarantined? | Inspect reason codes, then change inputs/profile |
| Clusters | Which task strings form useful categories, and which assignments need a human? | Review an uncertain sample or confirm a proposal |
| Slices | What named filter selects a build-ready set? | Inspect impact or continue to a build |
| Builds | What immutable dataset was produced and what is its lineage? | Inspect/download the export |
| Artifacts | Which content-addressed files exist and where are they referenced? | Verify or retrieve an artifact |
| Incidents | What deterministic signal needs acknowledgement or resolution? | Read cited evidence and act |
| Metrics | Where is time spent and how is the system trending? | Attribute latency to a pipeline operation |
| Schema | What does the live catalog actually contain? | Inspect tables/constraints |

The stage-3 gaps remain visible: a resources-focused operator view, experiment/run records, and benchmark results are not yet complete UI journeys. This design does not call those pages done merely because adjacent telemetry or build surfaces exist.

## Workflow to design around

1. Load a real LeRobot/MCAP source and see its job result, episode metadata, quality, and any validation failures.
2. On Clusters, compare the largest task classes by **episode count, distinct task-string count, and share of the corpus**. The size encoding and the printed number must agree.
3. Inspect a review candidate when the assignment is weak: an unsupported singleton or a core near the configured clustering radius. Select one or more task strings and either name a human-defined class or dismiss them as not a useful class. A class is a human annotation; it does not mutate source strings, episodes, or the automatic proposals.
4. Confirm a coherent automatic proposal, then use the existing curation/build/export path. Confirmation must remain reversible and stable across rebuilds.
5. If anything fails, the operator can follow reason codes and job/run lineage rather than infer from a green badge.

The human review list is a *triage queue*, not a label-confidence claim. Distance to the final centroid is an explainable heuristic, not calibrated probability; singletons are marked “no supporting neighbors,” not assigned a fabricated percentage.

## Design evidence (accessed 2026-10-01)

- **Visual hierarchy:** Nielsen Norman Group, [Visual Hierarchy in UX](https://www.nngroup.com/articles/visual-hierarchy-ux-definition/), accessed 2026-10-01. The article ties hierarchy to contrast, scale, and proximity; it warns that equal emphasis everywhere and too many saturated colors erase priority. Applied here: one clear largest-class view; count labels next to bars; one accent for magnitude; secondary metadata in a quieter register.
- **Progressive disclosure:** Nielsen Norman Group, [Progressive Disclosure](https://www.nngroup.com/articles/progressive-disclosure/), accessed 2026-10-01. Show the primary workflow first and disclose advanced options only when requested. Applied here: group navigation by operator intent and keep less-common review history/rebuild settings out of the main decision surface where practical.
- **Robotics curation sequence:** Foxglove, [Data Search & Curation](https://foxglove.dev/blog/data-search-curation), accessed 2026-10-01. Its public workflow connects search, review/annotation, and curated datasets rather than treating charts as the outcome. Applied here: uncertain task strings lead to an explicit human action, and confirmed proposals remain distinct from human labels.
- **Form clarity and recovery:** W3C WAI, [WCAG 2.2: Labels or Instructions](https://www.w3.org/WAI/WCAG22/Understanding/labels-or-instructions.html) and [Forms Tutorial](https://www.w3.org/WAI/tutorials/forms/), accessed 2026-10-01. Every input needs a visible purpose/instructions; forms should be short, grouped, keyboard operable, validate input, and explain recovery. Applied here: text-labelled checkboxes, one explicit class label, bounded batch sizes, and an undo/reopen action.
- **Dashboard grouping:** Grafana, [Dashboard best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/), accessed 2026-10-01. The documentation endpoint was a client-rendered shell during this review; only its indexed description was available, so no unsupported detailed claim is attributed to it. The stronger sources above ground the concrete decisions.

## Principles and anti-goals

1. Put the decision before its explanation: counts and queue size first, then an interpretation, then supporting strings and controls.
2. A number and its visual encoding must be cross-checkable. Bars are proportional to episodes; raw counts, distinct task counts, and total-corpus share are printed.
3. Distinguish machine proposals, calibrated facts, and human annotations in words. No pseudo-probability, no silent label transfer, no auto-confirm.
4. Use one strong visual channel at a time. Keep the CRT/instrument palette and robot identity, but spend the theme accent on active navigation/actions and bar magnitude, not every box and heading.
5. Prefer semantic lists, tables, and forms over decorative geometry. No unexplained point cloud, decorative axes, glow, gradient hero, glass card, or marketing hero.
6. Preserve the existing no-JavaScript control path, keyboard reachability, escaping, explicit empty/error states, four themes, and bounded reads.
7. Navigation is grouped by the operator's work (curate, operate, catalog), not by implementation modules. Do not merge pages until observed user tasks show that two surfaces are routinely traversed as one.

## UI changes in this slice

- Replace the task-point projection and the fixed-intrinsic-width treemap with one fluid ranked class-size view.
- Add an uncertainty review queue based on singleton support and observed assignment distance relative to the run's radius; persist class/dismiss decisions separately from proposal confirmations.
- Group the twelve nav destinations into a small number of expandable, named work areas; keep status and the robot-data-engine wordmark recognizable.
- Keep readouts, proposals, controls, and history but tighten the order and full-width behavior of the Clusters page.

## Validation

Unit tests assert decision semantics, exact counts/percentages, escaping, keyboard labels, and full-width chart structure. Contract/integration tests exercise persistence and routes. `just ui-audit` passed all 52 loads (13 listed routes × four themes) on 2026-10-01. Its first pass exposed false positives from SVG SMIL animations named generically by Chrome; the audit now checks only explicit CSS animation names, and a repeat passed 52/52. The audit runs against a temporary API server and checks computed style only. Desktop/narrow screenshots, keyboard-only completion, real screen-reader use, and operator comprehension remain unverified. Evidence and limitations: [EXP-0008](../experiments/0008-production-baseline-workflow-smoke.md).
