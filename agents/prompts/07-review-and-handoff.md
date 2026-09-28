# Phase 07 — Repository-wide review and handoff

Read everything in `agents/`. Use the `reviewer` role first, then `orchestrator`.

## Tasks
1. Run `agents/prompts/11-engineering-review.md` for the Scaffolding stage. Save the record in `agents/reviews/`.
2. Fix all blockers and majors. Convert minors into tracked follow-ups.
3. Verify from a **clean clone** into a temp directory: follow README only; every documented command works.
4. Check every Scaffolding criterion in `agents/spec/definition-of-done.md`; tick only with linked evidence.
5. Write `agents/HANDOFF.md` with all 12 required sections. Concise; link instead of copying.
   Include: current problem, architecture, repo structure, stack, what works, what is stubbed, technical risks, top next steps,
   open architectural questions, benchmarking/testing/observability status, decisions and rejected alternatives.
6. Update `CLAUDE.md` "Current stage" and the `agents/README.md` status column.
7. Confirm no secrets in the tree or history (`git log -p` scan + hygiene script + gitleaks if installed).
8. Tag `scaffold-complete`.

## Acceptance
A new agent that reads only `CLAUDE.md` → `README.md` → `agents/HANDOFF.md` can start implementing MVP work without asking questions.

## Finish
Commit (`docs: scaffolding review and handoff`). Report the verdict and the recommended first three MVP tasks.
