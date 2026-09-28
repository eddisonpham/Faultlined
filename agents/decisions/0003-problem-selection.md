# 0003. Problem selection: robot episode data engine

- **Status:** accepted (owner approved 2026-09-28 at the phase-01 checkpoint)
- **Date (UTC):** 2026-09-28
- **Deciders:** researcher + architect agents (proposal); project owner (approval)

## Context

The specification ([00-original-specification.md](../spec/00-original-specification.md)) requires one concrete robotics ML
infrastructure problem, selected after industry research and before any application code. Phase-01 research logged 40
sources across 14 organizations ([source-log.md](../research/source-log.md)) and synthesized recurring patterns
([industry-patterns.md](../research/industry-patterns.md)): the data layer — ingest, validate, index, version, curate,
serve with lineage — is the layer every surveyed organization hires for and tooling vendors commercialize. Hardware is a
single-laptop Windows box with one 8 GB RTX 5060, no Docker ([../spec/environment.md](../spec/environment.md)), which
rules out simulation-centric and cluster-centric stories. Scope guards forbid quantization/TensorRT/ONNX/edge themes and
duplicating hand-perception / VLA-quantization / grasp-deployment projects.

## Options considered

Full weighted scoring (weights fixed before scoring): [problem-candidates.md](../research/problem-candidates.md).

| Option | Pros | Cons |
|---|---|---|
| **A. Robot episode data engine** (selected, 4.95/5) | Exact match to hiring patterns (PI data systems, NVIDIA OSMO data factory, Foxglove curation, Waymo data systems); CPU/disk-bound → fits hardware; highly measurable (throughput, latency, reproducibility); demo-able on public MCAP/LeRobot/OXE data; adopts ecosystem formats instead of inventing them | Broad surface (formats + jobs + lineage + UI) requiring disciplined staging; some may read "data engineering" as less glamorous than training infra |
| B. Policy evaluation & regression platform (4.00/5) | Strong demand ("the missing middle", RoboLab/RoboDojo/XPolicyLab/robot-ci); maximal measurability | Credible policy evaluation needs simulation (Isaac Lab) that cannot run on captured hardware; degrades to replay-eval; depends on versioned scenario datasets it doesn't own |
| C. Fleet data mining / trigger service (4.05/5) | Real flywheel pattern; feasible with small embedding models | Consumes the curated catalog that A builds; retrieval-quality evaluation is fuzzy; building it first means building A's hardest half without A's contract |
| D. Multi-tenant GPU job scheduler (3.45/5) | Universally needed pattern (OSMO proves it) | At one GPU there is no multi-tenant scheduling problem; reinvents Ray/Kubernetes (explicitly warned against); weak interview differentiation |

## Decision

Adopt **A: robot episode data engine** — ingest raw episodes (MCAP / LeRobot) → validate against declared rules →
index/version in a catalog with lineage → serve reproducible curated dataset builds to workloads — with:

1. C's metadata/quality **query-and-select** surface carved into A (curation requires querying); embedding-based mining
   recorded as an extension, not core.
2. B adopted as the designed-for **next consumer workload** once dataset versioning is real (stage ≥ MVP).
3. A local job lifecycle (submit/queue/run/retry/timeout/cancel) built as a component over Postgres — not a
   general-purpose scheduler (option D's salvageable core).

## Consequences

**Positive**

- Interview signal aligns with source-log evidence per company ([industry-patterns.md](../research/industry-patterns.md) §3).
- Format adoption (MCAP, LeRobot v3, Parquet/Arrow) gives free interop and public test corpora ([technology-matrix.md](../research/technology-matrix.md)).
- Everything is measurable on one machine; benchmarks degrade gracefully without GPU.

**Negative / harder**

- Must resist scope creep into "generic data platform"; every feature needs a robot-episode justification.
- Windows-first dev + no Docker constrains deployment tiers (documented in [environment.md](../spec/environment.md)).
- Evaluation-platform expectations (B) must be managed: we ship data contracts first, eval second.

**What must be updated:** `agents/spec/problem.md` (written), `CLAUDE.md` opening line, `agents/README.md` status column,
technology-matrix classes if the owner selects a different candidate.

## Docs updated

- [../spec/problem.md](../spec/problem.md) — full problem definition (all 9 sections)
- [../research/problem-candidates.md](../research/problem-candidates.md) — weighted scores
- [../research/industry-patterns.md](../research/industry-patterns.md), [../research/technology-matrix.md](../research/technology-matrix.md), [../research/source-log.md](../research/source-log.md)
- [../spec/environment.md](../spec/environment.md) — captured environment
- `CLAUDE.md`, [../README.md](../README.md) — one-line problem statement / status columns
