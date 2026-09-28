# Technology Matrix

Classification: **core** (project cannot credibly work without it), **optional** (adopt only when a measured need appears),
**excluded** (considered and rejected — say why). A technology appearing in a job posting is not a justification.

Seed list verified 2026-09-28 against [source-log.md](source-log.md); all cells filled. Additions beyond the seed list are
marked *(added)*. Classes reflect the architecture planned for the selected problem ([../spec/problem.md](../spec/problem.md)):
a robot-episode **data engine** (ingest → validate → index/version → curate → serve) on the captured hardware
([../spec/environment.md](../spec/environment.md): single 8 GB GPU laptop, Windows/Git Bash, **no Docker**, 24-core CPU).

## Data / robotics formats & tools

| Technology | Purpose | Evidence / source (source-log #) | Relevance to this project | Class | Why justified (or not) — not keyword stuffing |
|---|---|---|---|---|---|
| LeRobot dataset format (v3) | Curated episode storage: Parquet + MP4, metadata with episode boundaries/tasks/normalization | #34, #35 | Primary **curated** format in and out of the engine; public datasets (OXE, LeRobot hub) are available in it | **core** | The de-facto standard for robot-learning data; adopting it gives interop + demo data for free. Inventing a format would be the classic anti-pattern flagged in #23 |
| MCAP | Raw multimodal log container (append-only, indexed, serialization-agnostic) | #20, #36 | Primary **raw-ingest** format (episodes as sensor logs); default ROS 2 log format | **core** | Crash-safe append-only writes and indexed reads fit ingestion; ecosystem readers in 6 languages (#36) |
| ROS 2 bags | Robotics recording format | #36 (MCAP is default ROS 2 log format) | Source systems record .bag; the engine must accept them | **optional** | Support a reader adapter after MVP; ROS 2 bag files increasingly *are* MCAP, so the marginal need is shrinking |
| Foxglove | Robotics data visualization / observability platform | #20, #21, #22 | Users will want to eyeball raw episodes | **optional** | Adopt as a *viewer* integration later; its data platform is a competitor/complement, not a dependency |
| Rerun | Multimodal viewer + data layer (chunked Arrow storage) | #23, #38 | Candidate viewer and, longer-term, storage-backend reference | **optional** | Its column-chunk design (#23) directly informs our storage layout; adopting the SDK as a viewer is cheap, adopting .rrd as *the* store would cede the LeRobot/OXE ecosystem |
| Open X-Embodiment / RLDS | Large standardized cross-embodiment dataset + schema | #26, #37, #35 | Demo/benchmark corpus + import adapter target | **optional** | OXE is 1M+ trajectories — too large for 228 GB free disk; use small subsets as fixtures, full import is deferred |
| Open X-Embodiment *format conversion* | Pooling 60 datasets into one schema (#26) | #26, #35 | Validation that our "curated build" abstraction matches industry practice | *(n/a)* | Evidence, not a dependency |
| Hugging Face Hub (`huggingface_hub`) | Dataset download source | #34, #35 | How demo datasets are fetched; `HF_KEY` already reserved in `.env.example` | **optional** | Only needed once we pull real datasets in phase 05; API key from env, never committed |

## Simulation / robotics platforms

| Technology | Purpose | Evidence / source (source-log #) | Relevance to this project | Class | Why justified (or not) |
|---|---|---|---|---|---|
| NVIDIA Isaac Sim | Robotics simulation + synthetic data generation | #14, #31, #32 | A possible future *producer* of episodes for the engine | **excluded** | Requires Linux + high-VRAM RTX GPU; cannot run on the captured hardware (8 GB, Windows). Would also drift the project toward simulation research, not infra |
| NVIDIA Isaac Lab | GPU-accelerated robot-learning framework | #32, #33 | Future eval-workload candidate | **excluded** | Same hardware wall as Isaac Sim; RoboLab (#33) already covers the eval-benchmark niche — we would duplicate, not add |
| Omniverse | Simulation/rendering platform | #14 | Underlies Isaac Sim | **excluded** | Not required by the data-engine problem at all |
| NVIDIA OSMO | Workflow orchestration for physical-AI workloads | #14, #31 | **Architectural reference**: data factory, lineage, YAML DAG I/O wiring, CI hooks | **excluded** (as dependency) | Needs Kubernetes backends (#31) — no Docker/K8s locally. We mirror its *semantics* (task DAGs, lineage) in a simpler local orchestrator and stay concept-compatible |
| GR00T tooling | Foundation-model training workflows | #14, #31, #17 | The flagship "workload" the platforms serve | **excluded** | Training GR00T is out of scope and infeasible locally; models are workloads, not our product (CLAUDE.md scope guard) |
| MuJoCo | CPU-friendly physics simulation | *(added — ecosystem knowledge)* | Could generate realistic synthetic episodes for demos/benchmarks | **optional** | A lightweight synthetic episode generator is enough for scaffolding; adopt only if fixture realism becomes a measured limitation |

## Orchestration / compute

| Technology | Purpose | Evidence / source (source-log #) | Relevance to this project | Class | Why justified (or not) |
|---|---|---|---|---|---|
| Ray | Distributed compute framework | *(seed)*; #10 (Waymo TPU/JAX workloads) | Tempting "big infra" for job execution | **excluded** | Our scale is one machine with one GPU; Ray's scheduler solves problems we don't have and would outsource the exact subsystem (queue/retry/cancel lifecycle) that is part of the product. Revisit per ADR if multi-node arrives |
| Kubernetes (+ Kueue/Volcano) | Cluster scheduling for ML jobs | #14, #31 (OSMO runs on K8s) | OSMO-style backend target | **excluded** | No Docker on the dev machine; cannot verify locally. Pure resume keyword stuffing at our scale |
| Argo Workflows | K8s-native DAG engine | *(seed)*; #14 (YAML multi-stage tasks) | Competes with our job engine | **excluded** | Ties us to K8s; our DAGs are shallow pipelines whose semantics we must own to make them benchmarkable |
| Slurm | HPC batch scheduler | *(seed)* | Not applicable | **excluded** | Designed for cluster/HPC queues; no cluster here |
| Airflow / Prefect / Dagster | General workflow orchestration | *(seed)* | Could run the pipeline DAGs | **excluded** | A general-purpose orchestrator would gut the project: the ingestion DAG lifecycle (submit, queue, retry, timeout, cancel) *is* our engineering deliverable (spec §4). Dependency weight unjustified at this stage |
| Temporal | Durable workflow execution | *(seed)* | Retry/durable-execution semantics | **excluded** | Powerful but heavy (server + SDKs + workflow determinism rules); MVP retry/timeout/cancel semantics are implementable directly against Postgres with tests |
| Postgres-backed job queue | Job lifecycle: submit/queue/run/retry/timeout/cancel | *(seed)*; MVP criteria | The engine's scheduler core | **core** | Exactly the lifecycle we must build; Postgres is already the catalog — one store, transactional job state, no broker |
| Celery / RQ | Task queue with workers | *(seed)* | Worker pool alternative | **excluded** | Requires a broker (Redis/RabbitMQ) — extra moving part with no gain over a small Postgres-backed worker pool we can test deterministically |
| Docker | Containerization / delivery | *(seed)*; environment | Production-like deployment tier | **optional** | Not installed locally (environment.md); provide Dockerfile/compose in a later phase as *optional* delivery, never required for dev/test |
| GitHub Actions | CI | *(seed)*; `.github/workflows/` exists | Runs fmt/lint/type/test/hygiene | **core** | Already seeded; required by definition-of-done; zero runtime cost |

## Storage / data

| Technology | Purpose | Evidence / source (source-log #) | Relevance to this project | Class | Why justified (or not) |
|---|---|---|---|---|---|
| PostgreSQL | Catalog: episodes, datasets, lineage, jobs, runs | *(added)*; #27 (lineage needs a queryable store), #14 (lineage) | System of record | **core** | The catalog/lineage/job-lifecycle needs transactional metadata; SQLite would be defensible but Postgres is the credible industry default and runs fine locally |
| Parquet / PyArrow | Columnar storage + zero-copy analytics | #23, #34 | Episode metadata tables, benchmark metrics, curated extracts | **core** | Columnar + sparse multi-rate data is the industry consensus (#23); LeRobot v3 *is* Parquet+MP4 (#34) |
| S3-compatible object storage (MinIO) | Artifact/blob store at scale | #14, #31 (object storage as task interchange) | Later: artifacts on object storage | **optional** | Local filesystem first (228 GB NVMe); add MinIO/S3 tier when multi-machine or size demands — OSMO shows the interchange pattern we'd follow |
| DuckDB | Ad-hoc SQL over Parquet lake | *(seed)* | Analyst-style queries over episode metadata; demo-able | **optional** | Nice query ergonomics for the curation UI; adopt when the catalog's SQL stops being enough |
| DVC | Git-based data versioning | *(seed)* | Versioning datasets | **excluded** | DVC is built around git + pointers for ML repos; our versioning is a *catalog* problem (content-addressed builds with lineage) over large binaries, where DVC's model fits poorly |
| lakeFS | Git-like semantics over object storage | *(seed)* | Data versioning alternative | **optional** | Relevant only if/when the S3 tier lands; premature now |
| Delta Lake / Apache Iceberg | ACID table format over object storage | *(seed)* | Lakehouse versioning | **excluded** | At our scale (single node, Parquet files + Postgres catalog) these bring heavy engines (Spark/Trino) for capabilities we don't need; trigger: object-store tier + concurrent writers |
| WebDataset | Sharded tar training format | *(seed)* | Training-input sharding | **excluded** | Training ingestion will consume LeRobot v3 / Parquet directly (#34); a third format adds conversion tax with no user |

## ML lifecycle

| Technology | Purpose | Evidence / source (source-log #) | Relevance to this project | Class | Why justified (or not) |
|---|---|---|---|---|---|
| PyTorch | Training/eval workloads consume datasets | #23 (dataloader on .rrd), #9 | Future workload-side integration | **optional** | The engine may *serve* tensors to a torch DataLoader later; not needed for the data-engine core. Models are workloads (CLAUDE.md) |
| PyTorch DDP/FSDP | Distributed training | *(seed)* | Training-scale concerns | **excluded** | Training infra is explicitly not our product; 1 GPU anyway |
| MLflow | Experiment tracking, model registry, lineage | #27 | Experiment/run records | **optional** | Run/lineage records are part of the problem we're building (we own the schema per observability conventions); adopt MLflow as a *backend* only if our thin records prove insufficient — decision deferred to MVP, tracked in backlog |
| Weights & Biases | Hosted experiment tracking | *(seed)* | External tracker | **optional** | Hosted SaaS conflicts with reproducible local-first scaffolding; same deferral as MLflow |
| Hugging Face Hub (model/dataset registry) | Publishing curated datasets | #34, #35 | Distribution channel for curated builds | **optional** | Stretch goal; not required by the core loop |
| NVIDIA DCGM / NVML (`pynvml`) | GPU utilization/VRAM telemetry | *(seed)*; environment (RTX 5060 present) | Resource telemetry required by definition-of-done | **core** | The only lightweight way to read GPU metrics on this box; `pynvml` degrades gracefully when no GPU — exactly the required behavior |

## Observability

| Technology | Purpose | Evidence / source (source-log #) | Relevance to this project | Class | Why justified (or not) |
|---|---|---|---|---|---|
| Structured logging (stdlib `logging` + JSON formatter) | Correlation IDs, job/run/episode IDs in logs | *(seed)*; observability conventions | Scaffolding requirement | **core** | No dependency needed to emit structured JSON; conventions live in `agents/observability/` |
| structlog / loguru | Logging ergonomics | *(seed)* | Alternative to stdlib | **optional** | Stdlib JSON formatter suffices at our volume; adopt only if logging ergonomics become a measured friction |
| OpenTelemetry | Traces/spans across components | *(seed)* | Cross-component tracing | **optional** | Correlation-ID propagation is implementable manually now; OTel spans earn their place when there are ≥3 services to trace (currently a monolith + workers) |
| Prometheus | Metrics exposition | *(seed)* | Scraped metrics | **optional** | Benchmarking stores metrics per `agents/benchmarking/metric-schema.md` (file-based) first; add a `/metrics` endpoint when a scraper exists |
| Grafana | Dashboards | *(seed)* | Visualize metrics | **optional** | The project's own UI covers inspection at this stage; Grafana is for ops fleets we don't run |
| Tempo / Jaeger | Trace backends | *(seed)* | Distributed tracing store | **excluded** | No traces yet (see OpenTelemetry); a trace backend before a tracer is pure accumulation |

## Serving / API

| Technology | Purpose | Evidence / source (source-log #) | Relevance to this project | Class | Why justified (or not) |
|---|---|---|---|---|---|
| FastAPI | HTTP API for the platform | *(seed)* | The platform's API surface | **core** | Standard, typed (Pydantic), testable via OpenAPI contract tests — fits the "API contract tests" MVP criterion with one framework |
| gRPC / protobuf | RPC + schema'd streaming | *(seed)* | Streaming ingest APIs | **excluded** | HTTP/JSON + file-based artifact exchange covers current needs; MCAP already handles serialized streams (#36). Trigger: real-time streaming ingest requirement |
| NVIDIA Triton | Model serving | *(seed)* | Inference serving | **excluded** | Serving/optimization is scope-guarded (CLAUDE.md); Triton is a workload we might *submit to*, never our core |

## Frontend (decide in phase 12)

| Technology | Purpose | Evidence / source (source-log #) | Relevance to this project | Class | Why justified (or not) |
|---|---|---|---|---|---|
| React + Vite + TypeScript | Engineering UI (jobs, datasets, lineage, benchmarks) | *(seed)*; #33 (RoboLab results dashboard), #22 | Required UI is a real deliverable (spec §4) | **optional** | Strong default, but selection is owned by the phase-12 frontend research; class locked until then |
| Tailwind | UI styling | *(seed)* | Same | **optional** | Decide at phase 12 |
| TanStack Query | API data fetching | *(seed)* | Same | **optional** | Decide at phase 12 |
| uPlot / ECharts / Recharts | Latency/throughput charts | *(seed)*; #33 (cross-experiment analysis dashboard) | Benchmark visualization | **optional** | Decide at phase 12; charting needs are modest (time series + histograms) |
| pnpm | Frontend package manager | *(seed)* | Tooling | **optional** | Only once a JS package tree exists |

## Languages & delivery

| Technology | Purpose | Evidence / source (source-log #) | Relevance to this project | Class | Why justified (or not) |
|---|---|---|---|---|---|
| Python (3.14) | Implementation language | #34, #35, #38 (ecosystem is Python-first); environment | Entire backend/worker/bench stack | **core** | The robotics-ML data ecosystem is Python; the problem is I/O- and orchestration-bound, not hot-loop-bound |
| C++ | Performance-critical paths | *(seed)*; #36 (MCAP has C++ core) | Only if a measured hot path demands | **excluded** (for now) | No measured hot path; also no g++ on the captured toolchain (environment.md). Re-benchmark before considering |
| Rust | Structural/perf justification | *(seed)*; #36, #38 (Rust SDKs exist) | Possible for throughput-critical readers | **excluded** (for now) | PyArrow/MCAP Python readers cover expected throughput; a second language adds build complexity for unmeasured gain. Trigger: profiling shows reader-bound bottleneck (record in experiments/) |
| uv | Python packaging/venv/runner | *(seed)*; #33 (RoboLab uses uv) | One-command setup/fmt/lint/test | **core** | Fast, Windows-friendly, lockfile-based reproducibility; matches how modern robotics repos ship (#33) |
| docker-compose | Local multi-service runs | *(seed)* | Production-like tier | **optional** | With Docker absent locally it stays a documented optional tier |

## Resume-keyword stuffing call-outs

Explicitly **not** adopted to look impressive (see classes above): Kubernetes/Kueue, Ray, Argo, Slurm, Airflow/Prefect/Dagster,
Temporal, Kafka*(not even evaluated — no streaming requirement)*, Triton, ONNX/TensorRT (also scope-guarded),
microservice-per-component decomposition, Delta/Iceberg lakehouse, Jaeger/Tempo. Each would add "buzzwords per README line"
while outsourcing or inventing problems the specification says we must engineer ourselves (job lifecycle, lineage,
benchmarking). The matrix above records the *trigger* that would make each of them legitimate.

## Verification notes

- Every seed-list technology has a row; none left at "considered" without a class.
- *(added)* rows: PostgreSQL, Postgres-backed job queue (seed wording kept), MuJoCo, PyArrow (folded into Parquet row).
- Classifications assume the selected problem in [problem-candidates.md](problem-candidates.md). If another candidate is
  chosen (owner approves ADR 0003), re-verify rows tagged: Isaac Lab, PyTorch, gRPC, DVC/lakeFS.
