# Phase 02 — Requirements and technology evaluation

Read `CLAUDE.md`, `agents/README.md`, `agents/spec/problem.md`, `agents/spec/environment.md`, `agents/research/*`, ADRs 0001–0003.
Use the `architect` role (and `researcher` if evidence is missing). **No application code.**

## Tasks
1. Write `agents/spec/requirements.md` (all sections listed in its stub):
   IDs for functional and non-functional requirements; numeric targets marked **provisional**; failure-mode catalog;
   data contracts; reproducibility requirements; constraints from the hardware; deferred items.
   Every requirement traces to a persona need from `problem.md`.
2. Write `agents/architecture/technology-decision-matrix.md` covering each major choice below. Score every candidate 1–5 on:
   industry relevance, technical necessity, performance, local feasibility, complexity (lower is better — say so), maintainability, resume relevance.
   State weights. Necessity and local feasibility must be able to veto a high-scoring option.
   Choices to cover: primary language(s) (and whether any C++/Rust is justified *now* or deferred with a measured trigger);
   API style/framework; job queue/scheduler (build-thin vs. Ray vs. Kubernetes/Argo vs. Postgres-backed queue…);
   metadata store; artifact/object storage and data formats; dataset & model versioning + lineage; experiment tracking (build vs. adopt);
   observability stack; frontend framework; containerization/compose; CI.
   **Prefer the simplest option that meets requirements on the captured hardware.** "Adopt an existing tool" and "build a thin layer"
   are both legitimate — justify with the problem, not with keywords.
3. Write one ADR per significant choice (`0004+`), including a rejected-alternatives table.
4. Finalize the core/optional/excluded column of `agents/research/technology-matrix.md` to match the ADRs.
5. Tick applicable definition-of-done items.

## Acceptance
- Every requirement has an ID and rationale; every provisional number has a "how we'll validate" note.
- Every choice in the matrix has an ADR or an explicit "deferred until <trigger>" entry.
- Nothing in the "core" list lacks a structural justification. `python scripts/check_repo_hygiene.py` passes.

## Finish
Commit (`docs: requirements and technology decisions`). Summarize the stack and the two riskiest decisions for me.
