# 0004. Language and engineering toolchain: Python-first, C++/Rust deferred

- **Status:** accepted
- **Date (UTC):** 2026-09-28
- **Deciders:** architect agent (phase 02), per owner-approved ADR 0003

## Context

The data engine ([../spec/problem.md](../spec/problem.md)) is I/O- and orchestration-bound: ingest, validate, index,
build. Requirements demand one-command setup/fmt/lint/typecheck/test/bench/run (NFR-011) on a Windows laptop with
Python 3.14, Node 24, Rust 1.98, CMake 4.1 and **no g++** ([../spec/environment.md](../spec/environment.md)). The target
ecosystem (LeRobot #34, MCAP Python readers #36, Rerun #38, PyArrow) is Python-first
([../research/source-log.md](../research/source-log.md)). Scored matrix: [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §1, §11.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **Python 3.14 + uv + pytest/ruff/mypy + GitHub Actions (chosen)** | Ecosystem-native; fastest iteration; typed via Pydantic/mypy; uv lockfiles give reproducible envs; CI already seeded | GIL limits CPU-parallelism (mitigate with process workers); interpreter overhead in hot loops |
| Rust now | Max throughput for readers; strong typing | No measured hot path justifies it; second toolchain on Windows; slower iteration; ecosystem adapters would be hand-written |
| C++ now | MCAP core is C++ | **No compiler on the box**; worst iteration speed; unjustifiable without a measured bottleneck |

## Decision

Implement the platform in **Python 3.14**, tooling with **uv** (env/lockfile/runner), **pytest** (tests),
**ruff** (fmt+lint), **mypy** (types), CI on **GitHub Actions** (seeded at `.github/workflows/`). CPU-bound stages use
process-level parallelism in the worker pool (ADR 0005) rather than threads where the GIL binds.

**Deferred with measured trigger:** C++ or Rust extensions are adopted only if profiling in the Performance/Scaling stage
shows decode/read throughput below NFR-001 targets, recorded first as an experiment record in `agents/experiments/`.

## Consequences

- (+) Single toolchain; every command scriptable under Git Bash; contributors and agents can run everything locally.
- (+) Matches how surveyed teams ship tooling (RoboLab uses uv — #33).
- (−) If a decoder hot path emerges, we pay a later FFI cost; the trigger keeps the decision honest.
- (−) Python 3.14 is new — dependency wheel availability must be checked at tooling time (phase 04).

## Docs updated

- [../architecture/technology-decision-matrix.md](../architecture/technology-decision-matrix.md) §1, §11
- [../research/technology-matrix.md](../research/technology-matrix.md) (classes: Python/uv/GitHub Actions core; C++/Rust excluded-for-now)
