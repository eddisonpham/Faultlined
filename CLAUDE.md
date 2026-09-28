# CLAUDE.md

Robotics ML infrastructure platform. **The platform is the product; models are workloads it executes.**
The concrete problem: a **robot episode data engine** — ingest, validate, index/version, and curate robot episode
datasets (MCAP / LeRobot) with full lineage, serving reproducible dataset builds to ML workloads (see `agents/spec/problem.md`).

## Read first (in order)

1. [agents/README.md](agents/README.md) — map of all persistent project context
2. [agents/spec/definition-of-done.md](agents/spec/definition-of-done.md) — current stage + acceptance criteria
3. [agents/HANDOFF.md](agents/HANDOFF.md) — current status, risks, next steps
4. Your role file in [agents/roles/](agents/roles/) if you were assigned one
5. Once they exist: `agents/spec/problem.md`, `agents/architecture/overview.md`

Then read only what your task touches: `agents/implementation/`, `agents/testing/`,
`agents/benchmarking/`, `agents/observability/`, `agents/decisions/`.

## Non-negotiables

- **Secrets:** never write, print, log, or commit a credential. Read them from environment variables
  (`HF_KEY`). Do not open `.env`. `.env.example` holds placeholders only.
- **Docs before code:** problem → requirements → tech evaluation → architecture come before implementation.
  Follow the stage order in `agents/spec/definition-of-done.md`.
- **ADR before architecture change:** record it in `agents/decisions/` first. Supersede ADRs; never silently edit history.
- **Knowledge lives in the repo:** update docs in the same commit as the change. Nothing important exists only in chat.
- **Smallest useful change:** no speculative abstractions, no technology without a justification in
  `agents/research/technology-matrix.md` or an ADR. Every dependency must earn its place.
- **Scope guard:** quantization / TensorRT / ONNX / edge-model optimization is NOT the central contribution.
  Do not duplicate hand-perception, VLA-quantization, or grasp-deployment themes.
- **Green before commit:** format, lint, type-check, tests, and `python scripts/check_repo_hygiene.py`.
- **Measure before optimizing.** Benchmarks follow `agents/benchmarking/methodology.md`; results become experiment records.
- Comments/docstrings: minimal and useful. Don't explain obvious code.

## Working loop

```text
Read context → understand current architecture → pick the smallest useful change → implement → test
→ benchmark when relevant → review → update docs/experiments/decisions → commit cleanly
```

## Where things go

| Need | Location |
|---|---|
| Requirements, problem, stage criteria | `agents/spec/` |
| Design, APIs, data flow, repo layout | `agents/architecture/` |
| Industry research, sources, tech matrix | `agents/research/` |
| Coding standards, status, slice definitions | `agents/implementation/` |
| Test strategy and failure-mode catalog | `agents/testing/` |
| Benchmark method, schema, backlog | `agents/benchmarking/` |
| Logging/metrics/tracing conventions | `agents/observability/` |
| Experiment records (current / tested / rejected / deferred) | `agents/experiments/` |
| Engineering reviews | `agents/reviews/` |
| ADRs | `agents/decisions/` |
| Agent role definitions | `agents/roles/` |
| Phase prompts (run in order) | `agents/prompts/` |

## Commits

Conventional commits (`feat:`, `fix:`, `docs:`, `test:`, `perf:`, `chore:`, `refactor:`). One logical change per commit.
Never commit large data, model weights, or `.env`.

## Current stage

Scaffolding. Update this line when `agents/spec/definition-of-done.md` advances.
