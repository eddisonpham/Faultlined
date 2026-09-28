# Phase 05 — Minimal vertical slice

Read `CLAUDE.md`, `agents/architecture/*`, ADRs, standards, `agents/implementation/status.md`.
Use `implementer`, `test-engineer`. **Intentionally minimal.**

## Tasks
1. First write `agents/implementation/vertical-slice.md`: the *smallest* path through the real architecture that proves the components communicate
   (e.g. submit one job through the API → queue → a worker runs a trivial workload on a tiny sample episode → artifact stored →
   metadata + lineage recorded → structured logs with a correlation ID → result visible via the API and one minimal UI page or CLI view).
   List explicitly what is out of scope. Get it consistent with `data-flow.md` and `api.md`.
2. Implement exactly that slice. Use a tiny synthetic episode generator for tests; if a small public robotics sample is useful, download it
   in an opt-in script (not in tests). `HF_KEY` may be read from the environment for Hugging Face access; never print or persist it;
   tests needing it must skip when absent.
3. Tests: unit for core logic; one integration test across real components; one end-to-end test through the public interface;
   **one failure-path test** (e.g. worker crash → job retried or failed with correct state). Update `testing/failure-modes.md`.
4. Work with real component boundaries — no throwaway prototype code. If the architecture doesn't survive contact, write an ADR
   and fix the docs first.
5. Update `implementation/status.md`, architecture docs (only where reality differs), and definition-of-done.

## Acceptance
- One command (`run` + a documented invocation) demonstrates the slice end to end from a clean clone.
- All gates green; coverage gate holds. Slice doc matches reality.

## Finish
Commit in small logical commits. Summarize what works, what is stubbed, and any architecture surprises.

Phase 05 implementation completed 2026-09-28: see [vertical-slice.md](../implementation/vertical-slice.md),
[implementation/status.md](../implementation/status.md), and commit history. PostgreSQL-backed integration/E2E tests
skip unless the operator supplies `DE_DATABASE_URL`; do not create or inspect credentials.
