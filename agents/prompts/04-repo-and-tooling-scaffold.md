# Phase 04 — Repository and engineering tooling

Read `CLAUDE.md`, `agents/architecture/*` (especially `repo-layout.md`), all ADRs, `agents/implementation/coding-standards.md`,
`agents/testing/testing-standards.md`. Use the `implementer` and `test-engineer` roles.

## Tasks
1. Create the source tree exactly per `repo-layout.md`: package skeletons, typed interface stubs and empty modules only.
   **No business logic yet.**
2. Toolchain (per ADRs): dependency management with lockfile; formatter; linter; type checker (strict where practical);
   test runner with markers (unit / integration / e2e / gpu / slow); coverage with an **enforced threshold** — pick an initial
   threshold that is honest for a skeleton and record the ratchet policy in an ADR.
3. One entry point for each of: `setup`, `fmt`, `lint`, `typecheck`, `test`, `bench`, `run` (Makefile/justfile/task runner — pick the simplest).
4. Config: typed settings loaded from files + environment; `.env.example` updated (placeholders only); `HF_KEY` is read from the environment only.
5. Containers: Dockerfile(s) and compose for the production-like local deployment described in `deployment.md`
   (GPU access optional and off by default).
6. CI (`.github/workflows/`): lint, typecheck, tests + coverage gate, hygiene script; separate optional job for slow/GPU tests.
   Keep `.pre-commit-config.yaml` in sync with the chosen tools.
7. Add tests for `scripts/check_repo_hygiene.py` using the project's test runner.
8. Fill in the "Commands" section of `agents/implementation/coding-standards.md` and the Quickstart in `README.md`.
9. Update `repo-layout.md` if reality diverged (with an ADR if it matters).

## Acceptance
- From a clean clone: `setup`, `fmt`, `lint`, `typecheck`, `test` all succeed with zero manual steps beyond installing documented prerequisites.
- Coverage gate demonstrably fails when tests are removed (verify once, don't commit the breakage).
- No secrets, data, or weights committed. Hygiene script passes.

## Finish
Commit in logical pieces (`chore: …`). Report the commands I should run to verify on my machine.
