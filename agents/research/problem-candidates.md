# Problem Candidates

Scored 2026-09-28 against the selection criteria from
[01-scaffolding-instructions.md](../spec/01-scaffolding-instructions.md) and the phase-01 prompt. Scores are 1 (worst) – 5 (best).
Weights were fixed **before** scoring. Evidence column cites [source-log.md](source-log.md) entries.

## Criteria and weights

| # | Criterion | Weight | What a 5 means |
|---|---|---|---|
| C1 | Single concrete problem (coherent story) | 15% | One sentence describes the whole project |
| C2 | Infra, not research | 10% | Deliverable is a reliable system, not a model/algorithm |
| C3 | Interview signal for NVIDIA/Tesla/Amazon/Google robotics | 20% | Public material shows these teams build exactly this |
| C4 | Feasibility on captured hardware | 15% | Works well on 1× RTX 5060 8 GB, 24-core CPU, Windows, no Docker ([environment](../spec/environment.md)) |
| C5 | Measurability (benchmarks) | 15% | Throughput/latency/quality metrics are natural, repeatable |
| C6 | Demo-ability | 10% | A convincing end-to-end demo is possible with public data |
| C7 | Distinctness from my existing projects | 10% | No overlap with hand perception / VLA quantization / grasp deployment; no edge-optimization theme |
| C8 | Scope risk (low risk = high score) | 5% | Can be staged into small vertical slices without dead ends |

## Candidates

| Candidate | C1 (15%) | C2 (10%) | C3 (20%) | C4 (15%) | C5 (15%) | C6 (10%) | C7 (10%) | C8 (5%) | **Weighted** |
|---|---|---|---|---|---|---|---|---|---|
| **A. Robot episode data engine** — ingest raw episodes (MCAP / LeRobot) → validate → index/version → curated dataset builds with lineage → feed workloads | 5 | 5 | 5 | 5 | 5 | 5 | 5 | 4 | **4.95** |
| B. Policy evaluation & regression platform — schedule batched policy evaluations/replays across versioned scenarios; compare runs; gate regressions | 4 | 5 | 5 | 2 | 5 | 3 | 4 | 3 | **4.00** |
| C. Fleet data mining / trigger service — query episodes for failure clusters/rare events via metadata + embeddings; export curated sets | 4 | 5 | 4 | 4 | 4 | 4 | 4 | 3 | **4.05** |
| D. Multi-tenant GPU job scheduler for robotics workloads | 3 | 5 | 3 | 4 | 4 | 3 | 3 | 2 | **3.45** |

Weighted score = Σ (score × weight). Example check for A: 5·.15 + 5·.10 + 5·.20 + 5·.15 + 5·.15 + 5·.10 + 5·.10 + 4·.05 = 4.95.

## Evidence per candidate

**A. Robot episode data engine** — the exact shape of roles at every surveyed org: Physical Intelligence "high-throughput
pipelines that validate, transform, and featurize raw multimodal data" (#9); NVIDIA OSMO "data factory to manage synthetic
and real robot data" with source→model lineage (#14, #31); Foxglove Data Search & Curation (#22); Rerun's data-layer thesis
(#23); Applied Intuition "wrangle autonomous vehicle data" (#11); Waymo "petabyte-scale data systems" (#10); format
standardization by OXE and LeRobot v3 (#26, #34). CPU/disk-bound → fits the hardware; metrics (ingest throughput, validation
latency, index build time, build reproducibility) are straightforward; public demo data exists (LeRobot hub, OXE subsets).

**B. Policy evaluation & regression** — strong demand signal (#28 "the missing middle", #29, #30, #39, OSMO nightly
regression #14) and maximal measurability. Fatal flaw for *selection*: credible policy evaluation runs in simulation
(Isaac Lab/RoboLab #33) which cannot run on our hardware (Windows, 8 GB VRAM, no Docker); it would degrade to
replay-based evaluation on recorded data — a weaker, less honest story. Kept as the primary **deferred consumer workload**
of A (it needs versioned scenario datasets first).

**C. Fleet data mining / triggers** — real pattern (Tesla flywheel triggers #40 secondary; Foxglove search/curation #22;
Rerun catalog queries #38) and feasible with small embedding models on 8 GB. Fatal flaw for *selection*: it is a *consumer*
of a curated, indexed episode catalog — building it first would mean building A's hardest half without A's contract.
Its non-fuzzy subset (metadata + quality-flag querying over the index) is absorbed into A's curation surface;
embedding-based mining is recorded as the natural extension.

**D. GPU job scheduler** — every platform needs one, and OSMO proves the pattern (#31), but the seed prompt itself warns
against reinventing Kubernetes/Ray, and at one GPU there is no scheduling problem to solve that isn't a general-purpose
one (Ray #excluded, K8s #excluded in [technology-matrix.md](technology-matrix.md)). Weak differentiation in interviews:
"yet another queue" reads as framework accumulation. A *local* job lifecycle (submit/queue/run/retry/timeout/cancel)
remains in scope as a component of A, which keeps the interesting parts without the reinvention.

## Decision

**Candidate A — robot episode data engine** — with two carve-ins that keep it one coherent story:

- C's metadata/quality **query-and-select** surface becomes part of A (curation without querying is not curation).
  Embedding-based mining stays an extension, not the core.
- B's batch evaluation/compare/lock-gate machinery is the designed-for **next consumer workload**, deferred until the
  dataset-versioning contract it depends on is real (definition-of-done stage ≥ MVP).

Recorded in ADR [0003](../decisions/0003-problem-selection.md). Full problem statement: [../spec/problem.md](../spec/problem.md).
