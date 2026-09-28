# Problem Definition

**Selected:** Robot episode data engine. Decided 2026-09-28 (UTC) — ADR [0003](../decisions/0003-problem-selection.md).
Candidates and weighted scores: [../research/problem-candidates.md](../research/problem-candidates.md).

## 1. Problem statement

Robotics ML teams collect robot episodes — multimodal sensor/action logs from arms, humanoids, and mobile robots — faster
than they can trust, index, and curate them: raw recordings arrive in mixed formats and mixed quality, quality checks and
metadata extraction are ad-hoc scripts, dataset assembly is manual and unversioned, and no one can say exactly which
episodes went into a training run. This project builds a self-hosted **robot episode data engine**: a platform that
ingests raw episodes (MCAP logs, LeRobot-format datasets), validates them against declared schemas and quality rules,
indexes them in a versioned catalog with full lineage, and serves **reproducible curated dataset builds** to downstream
training and evaluation workloads — with a real job lifecycle (submit, queue, run, retry, timeout, cancel), structured
observability, a benchmark harness, and an engineering UI.

## 2. Why this problem (evidence from `research/`)

Source numbers refer to [../research/source-log.md](../research/source-log.md); synthesis in
[../research/industry-patterns.md](../research/industry-patterns.md).

- **Every surveyed org hires for exactly this layer.** Physical Intelligence: "high-throughput pipelines that validate,
  transform, and featurize raw multimodal data" (#9). NVIDIA OSMO: "build a data factory to manage your synthetic and real
  robot data" with source→model lineage (#14, #31). Foxglove commercialized data search & curation (#21, #22). Applied
  Intuition: products that "wrangle autonomous vehicle data" (#11). Waymo: petabyte-scale data systems (#10).
- **The ecosystem is standardizing the formats this problem sits on** — MCAP (#20, #36), LeRobot v3 (#34, #35),
  Open X-Embodiment unified schemas (#26, #37) — so a data engine that adopts these contracts is interoperable rather than
  speculative.
- **Physical data defeats generic infrastructure.** Rerun's analysis: multi-rate + multimodal data is sparse and imbalanced
  per row, breaking conventional tables; metadata indexing + chunked storage is the emerging answer (#23).
- **It is feasible and measurable on the captured hardware** ([environment.md](environment.md)): ingestion/validation/
  indexing are CPU/disk-bound; benchmarks (throughput, validation latency, build reproducibility) are natural.

## 3. Users / personas and jobs-to-be-done

| Persona | Jobs to be done |
|---|---|
| **ML engineer (policy training)** | "Give me a versioned, curated dataset I can cite in a run record." Assemble dataset builds from queries over episodes; export in LeRobot v3; re-run a build and get identical content. |
| **Robotics data engineer** | "Turn today's robot recordings into trusted catalog entries." Ingest raw MCAP/LeRobot files, run validation, fix or reject failures, keep throughput high. |
| **Research engineer (evaluation)** | "Freeze the data a regression was measured on." Snapshot dataset versions for evaluation workloads; compare runs by lineage. |
| **Platform/infra engineer** | "Keep the engine observable and fast." Monitor job queues, worker health, GPU/CPU/disk telemetry; tune and benchmark the pipeline. |
| **(single-user dev)** | Everything above on one laptop; no cluster assumed. |

## 4. Inputs and outputs

**Inputs**

- Raw robot episodes: MCAP logs (#36), LeRobot dataset directories v2/v3 (#34). (ROS 2 bags later — matrix: optional.)
- Validation profiles: declarative schema/quality rules (required channels, frequency bounds, NaN/latency checks).
- Curation queries: metadata predicates (task, robot, date, quality flags, durations) selecting episodes for a build.
- Job requests via HTTP API: ingest / validate / index / build, with config and idempotency keys.

**Outputs**

- **Catalog** (Postgres): episodes, metadata, validation results, datasets, builds, lineage edges, jobs, runs.
- **Indexed metadata** (Parquet): frame/episode-level tables for fast analytical queries ([benchmarking](../benchmarking/)
  metrics recorded the same way).
- **Curated dataset builds**: content-addressed LeRobot v3 exports + a **lineage manifest** (source episode IDs + content
  hashes, validation profile + version, code commit, config, timestamps, seeds where applicable).
- **Run records & benchmark reports**: reproducibility fields per spec §9, stored in the format of
  `agents/benchmarking/metric-schema.md` (phase 06).
- **Observability streams**: structured JSON logs with correlation IDs (ingest_id / job_id / run_id / episode_id),
  resource telemetry (NVML-degradable), metrics.

## 5. End-to-end workflow

```text
robot recordings (MCAP / LeRobot)
        │  submit ingest job (API, idempotent)
        ▼
[ingest] copy/register raw artifacts ──► [validate] schema + quality rules ──► pass/fail + reason codes
        │                                        │
        ▼                                        ▼
[index] episode metadata + stats ──► catalog (Postgres) + Parquet metadata tables
        │
        ▼
[curate] query episodes by metadata/quality ──► selection
        │
        ▼
[build] content-addressed dataset build (LeRobot v3 export) + lineage manifest ──► artifact store
        │
        ▼
[serve] consumers: training workloads, evaluation workloads, UI, exports
        │
        ▼
[observe] logs / metrics / telemetry / benchmark records  (failure signals feed back into validation rules)
```

Every stage runs as a job in the lifecycle *submit → queue → run → retry → timeout → cancel*, with correlation IDs
propagating end to end.

## 6. Relationship to NVIDIA / Tesla / Amazon Robotics / Google Robotics patterns

| Pattern (industry) | Where it shows in public material | How this project relates |
|---|---|---|
| NVIDIA: workflow-orchestrated **data factory** with lineage source→model, YAML task DAGs, CI nightly regression (#14, #31) | OSMO | Our engine is the *data factory* layer: same semantics (task DAG I/O wiring, lineage manifests, CI-exposed benchmarks) implemented for a single machine without Kubernetes. Deliberate conceptual compatibility, no dependency (technology-matrix: OSMO excluded). |
| Tesla: **data engine / flywheel** — episodes selected by triggers, auto-labeled, curated back into training (#40 secondary; infra roles #4–6) | Optimus/Autopilot infra job families | We implement the non-ML core of the flywheel: ingestion, validation, selection-by-query, versioned builds. Trigger/embedding mining is the recorded extension. |
| Amazon Robotics/AWS: **fleet-scale data ops** + reference architectures for sim/real learning pipelines (#16–19) | DeepFleet, AWS Guidance | Metadata-first indexing and quality-gated ingestion mirror fleet-data practice; our deployment tier stays local-first (hardware reality). |
| Google/DeepMind/Waymo: **standardized formats + quality/productivity infra** — OXE unified schema (#26, #37), DeepMind "infrastructure quality" roles (#7, #8), Waymo data systems/CI for data generation (#10) | OXE, Gemini Robotics workloads (#24, #25) | Format contracts (MCAP, LeRobot v3) are adopted, not invented; validation and release discipline (tests, hygienic CI, regression benchmarks) are first-class deliverables. |
| Physical Intelligence (#9): validate → transform → featurize raw multimodal data | ML Infra (Data Systems) role | Our pipeline stages are a direct small-scale embodiment of this job description. |

## 7. Non-goals and scope boundaries

- **Not model research or training**: no VLA/policy development; models are workloads the platform serves (CLAUDE.md).
- **No quantization / TensorRT / ONNX / edge optimization** (CLAUDE.md scope guard) — never the central contribution.
- **No simulation platform**: Isaac Sim/Lab and OSMO are evidence and compatibility targets, not dependencies
  (they cannot run on the captured hardware).
- **No distributed cluster scheduling**: no Kubernetes/Ray/Airflow reinvention; the job lifecycle is a component, not a
  general-purpose scheduler ([technology-matrix.md](../research/technology-matrix.md) records the triggers that would
  change this).
- **No multi-tenant SaaS, no real-time robot control, no fleet telemetry ingestion** at this stage.
- **No auto-labeling ML**: validation is rule-based; learned quality/embedding mining is deferred (extension of candidate C).
- Distinctness guard: nothing duplicates hand perception, VLA quantization/edge deployment, or grasp deployment themes.

## 8. Success criteria

Preliminary criteria for the problem itself (ID'd FR/NFR with numeric targets are owned by
[requirements.md](requirements.md) from phase 02 — provisional until measured, per that document's rules):

1. A full workflow run (§5) executes end-to-end on real public episode data with every stage instrumented.
2. Curated builds are reproducible: re-running a build from recorded provenance yields an identical content hash.
3. Lineage is queryable: from any dataset build back to source episodes and validation results, and from any episode
   forward to builds containing it.
4. The job lifecycle (submit/queue/run/retry/timeout/cancel) is covered by tests including failure paths.
5. Performance is measured and baselined per [../benchmarking/methodology.md](../benchmarking/methodology.md)
   (ingest throughput, validation latency percentiles, index/build time, storage overhead).
6. The engine communicates through documented API contracts and an engineering UI (jobs, datasets, lineage, failures).

## 9. Candidates considered and why rejected

Full weighted table: [../research/problem-candidates.md](../research/problem-candidates.md); decision record:
[../decisions/0003-problem-selection.md](../decisions/0003-problem-selection.md).

- **B. Policy evaluation & regression platform** — rejected as the *primary* problem: credible policy evaluation runs in
  simulation (Isaac Lab) which the captured hardware cannot support; it would degrade to replay-based eval. Adopted as the
  designed-for next consumer workload (needs versioned datasets first).
- **C. Fleet data mining / trigger service** — rejected as the primary problem because it consumes the curated catalog
  that A creates; its metadata/quality query surface is absorbed into A; embedding mining recorded as an extension.
- **D. Multi-tenant GPU job scheduler** — rejected: at one GPU there is no genuine multi-tenant scheduling problem;
  building it means reinventing Ray/Kubernetes (explicitly warned against) and reads as framework accumulation.
- **A. Robot episode data engine — selected** (4.95/5 weighted; scores and weights in the candidates doc).
