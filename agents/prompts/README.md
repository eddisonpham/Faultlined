# Phase Prompts

Run in order. Use a **fresh session per phase** (`/clear` or a new `claude` process) — all continuity lives in the repo.
Invoke with `/phase NN` or paste the file. Every prompt ends with a commit; check `git log` between phases.

| # | Prompt | Human checkpoint |
|---|---|---|
| 01 | [research + problem selection](01-research-and-problem-selection.md) | **You approve the problem** (ADR 0003) |
| 02 | [requirements + technology evaluation](02-requirements-and-tech-evaluation.md) | skim ADRs |
| 03 | [architecture](03-architecture.md) | **You review architecture** |
| 04 | [repo + tooling scaffold](04-repo-and-tooling-scaffold.md) | commands run on the owner's Windows machine (phase 04 complete) |
| 05 | [minimal vertical slice](05-vertical-slice.md) | |
| 06 | [benchmark + observability foundations](06-benchmark-and-observability-foundations.md) | |
| 07 | [review + handoff](07-review-and-handoff.md) | scaffolding accepted |
| 10 | [iteration loop](10-iteration-loop.md) | reusable for MVP onward |
| 11 | [engineering review](11-engineering-review.md) | reusable at every stage gate |
| 12 | [frontend research + design](12-frontend-design.md) | before building the UI |
| 13 | [resume readiness](13-resume-readiness.md) | final stage |
| 14 | [production baseline and user-workflow stress](14-production-baseline.md) | stage-3 evidence before any production claim |

Tips
- If an agent asks a question a doc should have answered, fix the doc, not just the answer.
- If a phase produces something you disagree with, tell the agent to write an ADR superseding it rather than editing silently.
- Agents may spawn the role subagents in `.claude/agents/` (thin wrappers over `agents/roles/`).
