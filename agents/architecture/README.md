# Architecture

**STATUS: WRITTEN** (phases 02–03; implementation reconciled in phases 05–06, 2026-09-28). Diagrams use Mermaid. Decisions: [../decisions/](../decisions/) 0003–0013.

| File | Contents |
|---|---|
| overview.md | Purpose, context diagram, principles, top-level component map |
| components.md | Each component: responsibility, inputs/outputs, owner role, dependencies, failure behavior |
| data-flow.md | Data flow and control flow (sequence diagrams); job lifecycle state machine |
| api.md | Public and internal interfaces; resource model; error model; versioning; idempotency |
| storage.md | Metadata store, object/artifact store, formats, versioning, lineage model, caching |
| compute-orchestration.md | Queueing, scheduling, resource management, GPU allocation, retries/timeouts/cancellation, model-execution boundary |
| failure-handling.md | Failure modes → detection → recovery; links to `testing/` catalog |
| deployment.md | Local dev / production-like local / optional cloud; container layout; config |
| frontend.md | UI scope, information architecture, boundaries with the API (design research in phase 11) |
| technology-decision-matrix.md | Scored matrix (industry relevance, necessity, performance, local feasibility, complexity, maintainability, resume relevance) |
| repo-layout.md | Directory tree and where new code belongs |
| trade-offs.md | Major trade-offs with links to ADRs |
