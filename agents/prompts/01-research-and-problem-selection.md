# Phase 01 — Industry research and problem selection

You are starting a robotics ML infrastructure project. Read `CLAUDE.md`, `agents/README.md`,
`agents/spec/00-original-specification.md` and `agents/spec/01-scaffolding-instructions.md` completely.
Use the `researcher` and `architect` roles. **Write no application code in this phase.**

## Tasks

### 1. Capture the environment
Run read-only discovery (e.g. `uname -a`, `lscpu`, `free -h`, `df -h`, `nvidia-smi`, `docker --version`, `python3 --version`,
`node --version`, `rustc --version`, `g++ --version`, `cmake --version`). Do not open `.env`; do not print environment variables.
Fill `agents/spec/environment.md`. If GPU/VRAM/RAM is ambiguous or discovery fails, ask me once, then record the answer.

### 2. Research
Use web search and fetch. Cover current intern/new-grad ML-infrastructure, robotics-platform, data-infrastructure and
simulation-infrastructure roles and public technical material from **NVIDIA** (Isaac, GR00T, OSMO, DGX Cloud, Omniverse),
**Tesla** (Autopilot/Optimus data engine, training infra), **Amazon Robotics**, **Google/DeepMind Robotics**, plus others you judge relevant
(e.g. Waymo, Physical Intelligence, Figure, Boston Dynamics, Applied Intuition, Foxglove, Scale AI, Weights & Biases, Anyscale).
Sources: job postings, engineering blogs, docs, open-source repos (e.g. LeRobot, Ray, Argo, MLflow, MCAP, Rerun, Isaac Lab), talks, architecture write-ups.

Targets: ≥ 20 distinct sources, ≥ 6 organizations, ≥ 8 job postings. Log **every** source in `agents/research/source-log.md`
(URL, access date, key takeaways). Never fabricate a source; state when a page couldn't be retrieved.

### 3. Synthesize
- `agents/research/industry-patterns.md`: recurring problem types, architectural patterns, technologies with frequency
  (state how you counted), and a per-company mapping of what public material shows about their approach.
- `agents/research/technology-matrix.md`: every technology considered, all columns filled, classified **core / optional / excluded**.
  Verify the seed list there; add what you found. Default toward excluded/optional. Call out resume-keyword stuffing.

### 4. Select the problem
Write `agents/research/problem-candidates.md` scoring **at least four** candidates on: single concrete problem, infra-not-research,
interview signal for NVIDIA/Tesla/Amazon/Google robotics, feasibility on the captured hardware, measurability (benchmarks),
demo-ability, distinctness from my existing projects (hand perception, VLA quantization/edge deployment, hand-grasp deployment;
quantization/TensorRT/ONNX/edge optimization must not be central), scope risk. Use a weighted table and show the weights.

Seed candidates to evaluate (you may reject all and propose better):
- **A. Robot episode data engine** — ingest raw episodes (MCAP/ROS bag/LeRobot format) → validate → index/version → curated dataset builds with lineage → feed workloads.
- **B. Policy evaluation & regression platform** — schedule batched policy evaluations/replays on GPUs across versioned datasets/scenarios; compare runs; gate regressions.
- **C. Fleet data mining / trigger service** — query episodes for failure clusters/rare events using metadata + embeddings; export curated sets.
- **D. Multi-tenant GPU job scheduler for robotics workloads** — beware reinventing Kubernetes/Ray.
Choose **one** problem (a coherent combination is fine only if it remains one story). Do not choose based on what is fashionable.

### 5. Record
- `agents/spec/problem.md` — complete all sections listed there.
- `agents/decisions/0003-problem-selection.md` (ADR: candidates, scores, decision, consequences).
- Update `agents/decisions/README.md`, `agents/README.md` status column, and CLAUDE.md's opening paragraph with a one-line problem statement.
- Tick the Problem and Research items in `agents/spec/definition-of-done.md` only when true.

## Acceptance
- Source-log targets met; matrix has no empty cells; ADR 0003 exists; problem.md has no TODO sections.
- `python scripts/check_repo_hygiene.py` passes.

## Finish
Commit (`docs: industry research and problem selection`). Then **stop and summarize for me**: chosen problem, top 3 reasons,
the strongest rejected alternative, and any questions. I must approve before phase 02.
