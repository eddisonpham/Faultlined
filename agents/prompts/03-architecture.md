# Phase 03 — System architecture

Read `CLAUDE.md`, `agents/spec/*`, `agents/research/*`, all ADRs, `agents/architecture/README.md`.
Use the `architect` role. **No application code.**

## Tasks
Create every file listed in `agents/architecture/README.md`, using Mermaid for diagrams:

- `overview.md`: context diagram, principles, component map.
- `components.md`: per component — responsibility, inputs/outputs, dependencies, failure behavior, which role owns it.
- `data-flow.md`: control flow and data flow sequence diagrams; **job lifecycle state machine** (all states, transitions, who triggers each).
- `api.md`: public + internal interfaces, resource model, error model, versioning, idempotency keys, pagination, auth stance for local use.
  Model execution must be behind a narrow workload interface: models are workloads, not the product.
- `storage.md`: metadata schema outline, artifact layout, content addressing/versioning, lineage model, caching strategy.
- `compute-orchestration.md`: queueing, priorities, resource accounting, GPU allocation (including the no-GPU path), retries with backoff,
  timeouts, cancellation semantics, worker heartbeats, at-least-once vs exactly-once stance.
- `failure-handling.md`: table failure mode → detection → recovery → test.
- `deployment.md`: local dev vs production-like local vs optional cloud, respecting `environment.md`; config and secrets handling.
- `frontend.md`: scope and API boundary only (visual design happens in phase 12).
- `repo-layout.md`: directory tree, module boundaries, dependency direction rules, **where each new kind of code belongs**.
- `trade-offs.md`: top trade-offs with ADR links.

Also:
- Refine acceptance criteria for MVP → Resume/Demo Ready in `agents/spec/definition-of-done.md` so they reference real components.
- Fill `agents/implementation/status.md` with one row per component.
- Draft `agents/testing/failure-modes.md` mapping each failure mode to the component and planned test.
- Write ADRs for any decision made here that wasn't already recorded.

## Acceptance
- A new engineer can answer: what does it do, why, how do components interact, where does new code go.
- Every component in `components.md` appears in `repo-layout.md`, `status.md`, and the diagrams. No orphan components.
- No component exists without a requirement that needs it (cite the ID).
- `python scripts/check_repo_hygiene.py` passes.

## Finish
Commit (`docs: system architecture`). Summarize the architecture in ≤ 15 lines and list open questions. I review before phase 04.
