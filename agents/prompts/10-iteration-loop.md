# Iteration loop (reusable from MVP onward)

Task: `<describe the single task, or "highest-priority next step from HANDOFF.md">`

1. **Read context:** `CLAUDE.md`, `agents/spec/definition-of-done.md`, `agents/HANDOFF.md`, relevant architecture docs, ADRs, standards, status.
2. **Understand the current architecture** relevant to the task. Do not re-derive decisions already recorded; if you disagree, write a proposed ADR with evidence.
3. **Identify the smallest useful change.** State it in 2–3 lines with the acceptance test. Refuse scope creep.
4. **Implement** (correct roles). Follow coding standards. Prefer deleting code to adding code.
5. **Test:** unit + the relevant integration/contract/failure-path tests. Update `testing/failure-modes.md` if a mode is now covered.
6. **Benchmark when relevant** (perf-sensitive path or config change): hypothesis first, follow methodology, write an experiment record, update the registry.
7. **Review:** self-review the diff against `reviews/TEMPLATE.md`'s checklist; run format, lint, types, tests, coverage, hygiene.
8. **Update docs** in the same change: architecture (only if reality changed), status, experiments, decisions, HANDOFF next steps.
9. **Commit cleanly** (conventional commits; one logical change each). Report: what changed, evidence, what's next.

Stop and ask me if the task requires an architectural change that no ADR covers, a new dependency, or a scope change.
