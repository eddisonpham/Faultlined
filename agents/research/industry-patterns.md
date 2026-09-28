# Industry Patterns — Robotics ML Infrastructure

Synthesis of [source-log.md](source-log.md) (40 sources, 2026-09-28). Source numbers below refer to that log.
Claims about external systems cite the log entry; anything not in the log is marked as inference.

**How frequency is counted:** each distinct source counts at most once per pattern (a source "mentions/employs" a pattern if
its title, snippet, or fetched text demonstrates it). Job postings count as evidence that a *role exists*, weighted lower
than technical sources. Counts are therefore "number of sources exhibiting the pattern", not mentions.

## 1. Recurring problem types (with frequency)

| Problem type | Freq. (sources) | Evidence (source-log #) |
|---|---|---|
| Robot/multimodal episode **data ingestion, validation, curation** ("data factory") | 12 | #9 (PI: "validate, transform, featurize raw multimodal data"), #31/#14 (OSMO data factory, lineage), #21/#22 (Foxglove data platform, search & curation), #23 (Rerun data layer), #10 (Waymo petabyte data systems), #11 (Applied Intuition "wrangle AV data"), #34/#35 (LeRobot format + porting), #26/#37 (OXE unified format), #13 |
| **Workflow orchestration** across heterogeneous compute | 8 | #14/#31 (OSMO YAML DAGs, K8s backends), #16/#17 (AWS sim+training reference architectures, AWS Batch), #5 (Tesla sim infra role), #10 (Waymo CI/CD for data generation), #2, #6 |
| **Dataset versioning, lineage, reproducibility** | 7 | #14 ("lineage from source to trained model… traceability, reproducibility"), #27 (MLflow runs/registry), #23 (catalog + semantic metadata), #34 (versioned dataset format), #26 (standardized pooling), #31 |
| **Evaluation / regression harnesses & release gates** | 8 | #33 (RoboLab: tasks, success predicates, cross-experiment analysis), #29 (RoboDojo), #30 (XPolicyLab), #39 (robot-ci), #28 ("the missing middle"), #14 (OSMO "nightly regression testing, benchmarking, model validation"), #9, #6 |
| **Simulation-based data generation & SIL/HIL testing** | 6 | #14 (SDG, SIL/HIL), #31, #32 (Isaac Lab), #17 (AWS guidance), #5, #16 |
| **Observability for physical/multimodal data** (≠ server observability) | 5 | #20/#21/#22 (Foxglove), #23 (Rerun: time-aligned multi-rate streams), #38 |
| **Training infra**: distributed training, data loaders, eval pipelines | 6 | #6 (Tesla distributed training), #9 (PI training systems), #10 (Waymo TPUs/JAX), #31 (OSMO DGX+OVX RL loops), #23 (PyTorch dataloader on .rrd), #33 |
| **Fleet data & operational feedback loops** | 4 | #18 (Greengrass fleet mgmt), #19 (DeepFleet), #40 (Tesla flywheel, secondary), #22 |

## 2. Architectural patterns observed

1. **Data-engine loop / flywheel.** Fleet or lab robots emit episodes → triggers select interesting data → auto-label /
   validate → curated datasets feed training → failures are mined back into collection. (Tesla shadow-mode flywheel #40,
   secondary; Foxglove curation #22; PI data-quality role #9-family.)
2. **Standardized episode format as the integration contract.** The industry converges on a few open formats instead of
   bespoke schemas: MCAP as the raw log container (#20, #36 — default in ROS 2), LeRobot v3 (Parquet + MP4, rich metadata,
   #34/#35), OXE/RLDS unified schemas (#26/#37). A platform that speaks these formats interoperates with the ecosystem
   for free; a platform that invents its own format carries a permanent translation tax.
3. **Declarative multi-stage workflow DAGs with task-level I/O wiring.** OSMO: YAML tasks declare `inputs` from upstream
   task outputs; artifacts flow via task outputs or object storage (#14, #31). Workloads are defined once, scheduled onto
   heterogeneous backends.
4. **Chunked / columnar storage for multi-rate multimodal data.** Rerun's column chunks + Arrow record batches (#23):
   physical data is sparse across time and imbalanced across modalities; row-per-timestamp tables fail. Parquet underpins
   LeRobot v3 (#34). Generalizable rule: keep hot metadata columnar and queryable; keep bulky streams in sequential
   containers (MP4/MCAP) referenced by metadata.
5. **Metadata index + search as the curation surface.** Foxglove Data Search (#22), Rerun catalog server (#38), OXE
   unified metadata (#26): you curate datasets by querying an index (episode metadata, embeddings, quality flags), not by
   browsing raw files.
6. **Lineage/provenance records spanning source → dataset build → training run.** OSMO lineage for auditing (#14),
   MLflow run/registry lineage (#27), Rerun semantic metadata carried through pipelines (#23).
7. **Server-client policy architecture for evaluation.** RoboLab runs the model as a standalone server and connects a
   lightweight inference client (#33); XPolicyLab standardizes adapters (#30). Decouples harness from policy stack —
   the harness owns scenarios, metrics, and reporting.
8. **Nightly regression + benchmark gating in CI.** OSMO plugs into CI/CD for scheduled regression/benchmark/validation
   (#14); robot-ci is the academic miniature (#39). Benchmark results are treated as build artifacts that can fail a build.
9. **Reproducibility by pinning, not by hope.** RoboLab pins `env_cfg.json` per recording and documents that replay is not
   invariant across simulator versions (#33); MLflow records params/artifacts (#27); OXE fixes a single schema (#26).
   Provenance fields (config, data version, code commit, seeds) are recorded at run time, not reconstructed later.

## 3. Per-company mapping (public material only)

| Company | What public material shows | Sources |
|---|---|---|
| **NVIDIA** | Full-stack physical-AI infra: OSMO orchestrates SDG → training → RL → SIL/HIL eval on heterogeneous compute, with a "data factory", lineage, and CI hooks; Isaac Lab/Sim as simulation substrate; RoboLab as evaluation benchmark; GR00T as the flagship workload. Now open-sourcing the orchestration layer itself. | #1–3, #14–15, #31–33 |
| **Tesla** | Optimus/Autopilot run on dedicated infra job families: ML/RL infra for "high-volume rollouts", simulation infra, distributed-training infra with tooling that *measures* NN improvements; data flywheel (shadow mode, auto-labeling) documented secondarily. | #4–6, #40 |
| **Amazon Robotics / AWS** | Fleet-scale operations data feeds ML (DeepFleet multi-robot coordination model); AWS sells the reference architecture (sim + real learning, Batch-based GR00T fine-tuning, Greengrass fleet software delivery). Infrastructure is fleet-first. | #13, #16–19 |
| **Google DeepMind / Waymo** | DeepMind: robotics "infrastructure quality & productivity" roles (testing, release discipline, data engineering) plus Gemini Robotics VLA/ER models; OXE standardized 1M+ trajectories across 22 embodiments. Waymo: petabyte data systems, CI/CD for data generation, sim on TPUs, ML infra as a dedicated ladder. | #7–8, #10, #24–26, #37 |
| **Physical Intelligence** | Smallest company with the most explicit data-engine job: "high-throughput pipelines that validate, transform, and featurize raw multimodal data" + batch processing + data-quality roles; data systems for large-scale robot learning. | #9 |
| **Applied Intuition / Intrinsic** | AV/robotics data wrangling as product (Applied Intuition), data-infrastructure roles inside robotics software companies (Intrinsic). | #11–12 |
| **Foxglove / Rerun** | The tooling ecosystem itself commercializes the pain: MCAP format → data platform → search/curation; Rerun: viewer → unified data layer (chunked storage, catalog, dataloader). Both say web-data-era infrastructure fails on physical data. | #20–23, #36, #38 |

## 4. Inferences for this project (explicitly labeled as inference)

- **Inference:** the most defensible, hardware-feasible infrastructure problem on our 8 GB single-GPU Windows box is the
  *data engine* layer (ingest → validate → index → version → curate → serve), because it is CPU/disk-bound, measurable,
  and is the layer every company above is hiring for. Orchestration-of-training and simulation-based eval need GPU/OS
  resources we do not have ([../spec/environment.md](../spec/environment.md)).
- **Inference:** a *policy evaluation & regression platform* is the strongest second story and a natural *consumer* of a
  data engine (needs versioned scenario datasets first). It is deferred, not rejected — see
  [problem-candidates.md](problem-candidates.md).
- **Inference:** adopting MCAP + LeRobot v3 as boundary formats (rather than inventing one) buys interoperability with
  ROS 2, Foxglove, Rerun, and public datasets like OXE — a credible talking point with any of the mapped companies.
