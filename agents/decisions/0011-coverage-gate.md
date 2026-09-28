# 0011. Initial coverage gate and ratchet policy

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** implementer + test-engineer agents (phase 04)

## Context

Phase 04 requires an enforced coverage threshold, honest for an initial skeleton, and a ratchet policy. `pytest-cov` is
the test runner; tests are deterministic and CPU-only by default. Protocol definitions and data-only contract modules
have no runtime behavior to test. Coverage must encourage behavioral tests rather than line counting.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| No threshold until MVP | No false failures | Coverage can silently stay at zero; misses phase-04 acceptance |
| 90% immediately | Strong signal | Dishonest for protocol-only and data-contract modules; encourages test-for-number |
| **70% initial floor with explicit exclusions (chosen)** | Meaningful enforcement while keeping the measured surface behavioral | Excluded contract surfaces need meaningful structural/type checks |

## Decision

Enforce **70% total branch coverage** with pytest-cov. The floor may only **increase** as components become real; lowering
it requires a superseding ADR explaining the regression and remediation. Tests must assert behavior, not execute lines for
the number.

Exclude only modules/classes that are purely declarative and have no behavior: `Protocol` interfaces (`StageHandler`,
`EpisodeReader`, `Workload`, `Catalog`, `ArtifactStore`) and immutable data-only contract records (`JobContext`,
`StageResult`, `EpisodeSource`, `DatasetView`, `WorkloadResult`). Tests of implementations must cover their concrete
behavior; this exclusion does not exempt queue state transitions, manifest hashing, API validation, or handlers.
Coverage remains a floor, not a quality claim.

## Consequences

- (+) CI fails when executable behavior lacks tests; protocol declarations do not distort the denominator.
- (+) The gate can ratchet up when concrete modules replace stubs.
- (−) Structural interface compatibility is enforced by strict mypy and concrete integration tests, not coverage.
- (−) Initial 70% is not a mature-project quality badge; don't advertise it as one.

## Docs updated

- `pyproject.toml` (`tool.coverage.report.fail_under = 70`, declarative module exclusions)
- [../implementation/coding-standards.md](../implementation/coding-standards.md) Commands
- [../testing/testing-standards.md](../testing/testing-standards.md) coverage policy
