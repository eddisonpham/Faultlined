# Project Specification: Robotics ML Infrastructure Platform

> Canonical end-state specification, stored verbatim from the project owner. Do not edit.
> Refinements and overrides go in `agents/decisions/` (ADR) and the living docs in `agents/spec/`.

## 1. Objective

Design and implement an advanced, production-quality **ML infrastructure platform for robotics**, primarily targeting **ML Software Engineering / ML Infrastructure** roles rather than ML research.

The project should be technically adjacent to the types of systems built at:

1. NVIDIA
2. Tesla
3. Amazon Robotics
4. Google Robotics / DeepMind

You may expand the company set when useful for identifying relevant engineering patterns.

The project should solve **one concrete robotics infrastructure problem** rather than becoming a collection of unrelated technologies.

The final system should be sophisticated enough to serve as a strong **intern/new-grad resume project**, while remaining technically defensible and realistically implementable.

### Important constraints

* The project is primarily **software/infrastructure**, not a model-development project.
* Robotics + ML infrastructure must be the central theme.
* Models may be trained or fine-tuned only where useful for validating the infrastructure.
* Do **not** make quantization, TensorRT optimization, ONNX optimization, or edge-model optimization the central contribution. I already have projects covering these areas.
* I already have projects involving hand perception, VLA quantization/edge deployment, and hand grasping deployment, so avoid duplicating these themes.
* Python, C++, and Rust may be used where technically justified.
* Prefer simple architectures over unnecessary technology accumulation.

---

## 2. Industry Research

Before implementation, research current internship/new-grad-level engineering roles and publicly available technical material from NVIDIA, Tesla, Amazon Robotics, Google/DeepMind, and other relevant robotics/ML infrastructure companies.

Use:

* Job descriptions
* Engineering blogs
* Technical documentation
* Open-source repositories
* Technical talks/presentations
* Relevant public architecture discussions

Identify technologies, architectural patterns, and engineering practices relevant to robotics ML infrastructure.

For every technology considered, document:

* Technology
* Purpose
* Evidence/source
* Relevance to this project
* Whether it is **core / optional / excluded**
* Why it is justified rather than included merely as resume keyword stuffing

Do not assume that a technology belongs in the project simply because it appears in a job posting.

---

## 3. System Scope

Design a complete end-to-end platform. The infrastructure should support a realistic robotics ML workflow such as:

`robot/episode data → ingestion → validation → preprocessing → metadata/features → model workload → evaluation → artifact storage → experiment tracking → serving/execution → monitoring/UI`

The exact workflow should be determined by the selected problem.

The **platform/infrastructure itself** is the primary product. Models are workloads executed by the platform.

Define:

* Core problem
* Users/personas
* Inputs/outputs
* System requirements
* Architecture
* Major components
* Data flow
* APIs/interfaces
* Storage
* Compute/orchestration
* Failure handling
* Observability
* Evaluation
* Deployment model

---

## 4. Backend + Frontend

Build:

### Backend

A productionized backend exposing the platform's core functionality through clean APIs/interfaces.

Address where relevant:

* Job/pipeline execution
* Resource management
* GPU workloads
* Queuing/scheduling
* Concurrency
* Retries/timeouts
* Cancellation
* Fault tolerance
* Artifact management
* Dataset/model versioning
* Experiment tracking
* Caching
* Data lineage
* Reproducibility

### Frontend

Build a polished engineering-facing UI.

The UI should allow users to meaningfully inspect and operate the platform, such as:

* Jobs/pipelines
* System status
* GPU/resource utilization
* Experiments
* Dataset/model versions
* Pipeline failures
* Benchmark results
* Latency/throughput
* Artifacts
* Evaluation results

Research real engineering/platform UIs before choosing the visual design.

The frontend should be **clean, restrained, technically credible, and purpose-built**, not a generic AI-generated dashboard.

---

## 5. Production Engineering Requirements

Treat the repository as a real GitHub group project maintained by a professional software engineering team.

Code must be:

* Modular
* Maintainable
* Robust
* Efficient
* Simple
* Well-tested
* Production-oriented

Keep comments/docstrings minimal and useful. Avoid explaining obvious code.

### Every commit/PR should maintain:

* Passing tests
* Formatting/linting
* Type checking where applicable
* No obvious dead/duplicated code
* Clean architecture
* Updated documentation/specifications when required
* No broken functionality
* No secrets committed to the repository

Do not optimize for code volume. Prefer the smallest clean abstraction that solves the problem.

---

## 6. Testing

Use meaningful testing rather than artificially maximizing coverage.

Include appropriate:

* Unit tests
* Integration tests
* API/contract tests
* Black-box tests
* Failure-path tests
* End-to-end tests

Test realistic failure modes such as:

* Corrupt/missing data
* Worker failure
* GPU OOM
* Timeouts
* Network failure
* Duplicate jobs
* Partial pipeline execution
* Invalid configurations
* Missing dependencies

Establish a reasonable coverage target and enforce it automatically.

---

## 7. Benchmarking + Performance

Performance is a first-class concern.

Build a reproducible benchmarking system that measures, where applicable:

* Throughput
* P50/P95/P99 latency
* Queue time
* End-to-end latency
* GPU utilization
* CPU utilization
* VRAM/RAM usage
* Disk/I/O
* Network usage
* Startup time
* Failure rate
* Scaling behavior
* Cost/resource efficiency

Benchmarking must use controlled workloads, warmup runs, repeated trials, and clearly documented configurations.

Maintain baselines so performance regressions can be detected across implementations/configurations.

---

## 8. Observability + Metrics

Create a dedicated observability/logging capability.

Use structured logging and, where appropriate:

* Metrics
* Tracing
* Correlation/request/job IDs
* Resource telemetry
* Pipeline-level metrics
* Episode-level metrics
* Error/failure metrics

Benchmark and runtime metrics should be persisted in a format that makes comparison across experiments easy.

The agent should collect **as many useful metrics as practical**, but avoid metrics that provide no actionable information.

---

## 9. Reproducibility

Every experiment/benchmark should record enough information to reproduce the result:

* Git commit
* Configuration
* Dataset/version
* Model/version
* Hardware
* Software/environment
* Random seed where applicable
* Timestamp
* Metrics
* Relevant system parameters

Do not rely on undocumented manual state.

---

## 10. Repository / Agent Structure

The root should contain:

```text
CLAUDE.md
agents/
```

`CLAUDE.md` should point agents to the relevant material under `agents/`.

The `agents/` directory should contain all persistent project context, specifications, engineering standards, research findings, benchmarks, experiments, and decisions.

Suggested structure:

```text
agents/
├── spec/
├── architecture/
├── research/
├── implementation/
├── testing/
├── benchmarking/
├── observability/
├── experiments/
├── reviews/
└── decisions/
```

All agent-facing documentation should be Markdown.

Agents must continuously update these documents as the system evolves.

Do not allow agent knowledge to exist only in conversation history.

---

## 11. Agent Responsibilities

Multiple specialized agents may be created where useful.

Agents should have clearly defined responsibilities and should read existing project context before making changes.

Major architectural changes must be documented through an ADR/decision record before implementation.

Agents should avoid:

* Duplicating existing work
* Introducing unnecessary technologies
* Breaking existing interfaces without justification
* Creating speculative abstractions
* Leaving undocumented architectural decisions

Before declaring a milestone complete, perform a repository-wide engineering review.

---

## 12. Experimentation

The `agents/experiments/` directory should maintain reproducible records of:

* Configuration
* Hypothesis/purpose
* Changes
* Benchmark results
* Trade-offs
* Conclusions
* Follow-up actions

This information should allow future agents to understand **why** a configuration exists rather than blindly changing it.

Maintain a distinction between:

* Current production configuration
* Tested alternatives
* Rejected approaches
* Future/deferred ideas

---

## 13. Technology Selection

Create a technology decision matrix covering the major architectural choices.

Evaluate each candidate on:

* Industry relevance
* Technical necessity
* Performance
* Local feasibility
* Complexity
* Maintainability
* Resume relevance

Do not add infrastructure solely to demonstrate that a technology was used.

The final architecture should be sophisticated because of the **engineering problem**, not because it contains many frameworks.

---

## 14. Deployment / Environment

Design around the actual available development hardware and resources.

Do not assume access to unlimited GPUs or cloud infrastructure.

Clearly distinguish:

* Local development
* Production-like local deployment
* Optional cloud/distributed deployment

Cloud services may be used where justified, but the project must remain reproducible and practical for development.

Secrets must always be supplied through environment variables or secret-management mechanisms.

Example:

```text
HF_KEY=<set locally>
```

Never place actual credentials or API keys in source code, Markdown, Git history, or configuration committed to the repository.

---

## 15. Development Process

Follow this order:

1. Industry research
2. Problem selection
3. Requirements/specification
4. Technology evaluation
5. System architecture
6. Repository/agent scaffolding
7. MVP implementation
8. Testing infrastructure
9. Benchmarking infrastructure
10. Observability
11. Performance iteration
12. Production hardening
13. Frontend implementation/polish
14. Final engineering review
15. Resume-ready documentation and metrics

Do not begin by writing large amounts of code before the problem, requirements, and architecture are documented.

---

## 16. Final Deliverables

The completed repository should contain:

* Production-quality implementation
* Backend
* Frontend
* Tests
* CI/development tooling where practical
* Benchmarking framework
* Observability/logging
* Reproducible experiments
* Architecture documentation
* ADRs/engineering decisions
* Deployment instructions
* Developer setup instructions
* Troubleshooting/runbooks
* Resume-ready performance metrics

The final project should demonstrate strong **ML infrastructure + robotics + software engineering** ability and should be credible when discussed in an NVIDIA/Tesla/Amazon Robotics/Google Robotics interview.

Prioritize technical depth, clean engineering, measurable performance, and defensible design decisions over feature count.
