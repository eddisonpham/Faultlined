# Follow-Up Instructions: Scaffolding Phase

> Canonical scaffolding-phase instructions, stored verbatim from the project owner. Do not edit.

This is a **scaffolding/specification phase**, not the final completion of the project.

The previous specification defines the desired end-state. Your job now is to turn that specification into a **coherent, extensible foundation** that future coding agents can build on without repeatedly rethinking the project.

## 1. Core Principle

Do not prematurely build the entire platform.

At this stage, optimize for:

* Correct problem selection
* Strong architecture
* Clear boundaries/interfaces
* Repository structure
* Agent knowledge/context
* Technology decisions
* Development workflow
* Testing/benchmarking/observability foundations
* A minimal runnable vertical slice proving the architecture works

The scaffolding should make subsequent implementation straightforward.

---

## 2. Do Not Treat the Previous Specification as Perfect

You are allowed to challenge, refine, or remove anything from the previous specification when research or engineering reasoning suggests a better approach.

Do not blindly implement every requested technology.

When changing an important decision:

1. Explain the reason.
2. Record it in `agents/decisions/`.
3. Update the relevant specification/architecture documents.
4. Ensure future agents can understand the new canonical decision.

---

## 3. Definition of Done for the Scaffolding Phase

There is intentionally **no definitive “project complete” state yet**.

Instead, scaffolding is complete when the repository is in a state where another competent software engineer/agent can clone it, understand the architecture, and begin implementing the platform without needing to rediscover the project's fundamental decisions.

At minimum, scaffolding should establish:

### Problem

* A concrete robotics ML infrastructure problem has been selected.
* The intended users and workflow are documented.
* The relationship to NVIDIA/Tesla/Amazon Robotics/Google Robotics engineering patterns is documented.
* Major scope boundaries and non-goals are explicit.

### Research

* Relevant industry research has been completed to a useful initial depth.
* Sources are recorded.
* Technologies/patterns have been separated into core, optional, and rejected.
* The research is sufficient to justify the architecture.

### Architecture

* System architecture is documented.
* Major components and responsibilities are defined.
* Data/control flow is documented.
* Major interfaces/APIs are defined.
* Storage, compute, orchestration, model execution, observability, and frontend boundaries are established where relevant.
* Major architectural trade-offs are documented.

### Repository

The repository has a clean initial structure suitable for a real engineering project.

It should be possible to understand:

```text
What the system does
Why it exists
How the components interact
Where new code belongs
How to test it
How to run it
How to benchmark it
Where architectural decisions are recorded
Where agents should look for context
```

### Agent Infrastructure

`agents/` should already contain the foundational context needed by future agents.

Agents should not have to infer:

* Current architecture
* Current technology choices
* Project goals
* Coding standards
* Testing standards
* Benchmark methodology
* Observability conventions
* Known limitations
* Rejected approaches
* Current experiments

### Engineering Tooling

Establish the initial foundation for:

* Formatting
* Linting
* Type checking where applicable
* Testing
* Coverage measurement
* Local development
* Environment configuration
* CI where practical
* Containerization where appropriate

These systems do not need to be fully sophisticated yet. They need to be **correctly structured and extensible**.

### Vertical Slice

Implement a **very small end-to-end path** through the architecture.

It should demonstrate that the major pieces can communicate and that the architecture is viable.

This slice should be intentionally minimal.

Do not spend the scaffolding phase implementing every feature.

### Benchmarking

Create the initial benchmark framework and metric schema.

It is acceptable for many benchmark implementations to be placeholders initially.

The important thing is that future agents have a standardized way to add experiments and compare configurations.

### Observability

Create the initial logging/metrics/tracing structure and conventions.

Again, this does not need to represent the final observability system yet.

It should establish the interfaces and conventions that future implementation can follow.

---

## 4. What Should NOT Be Considered Necessary Yet

Do not force the scaffolding phase to complete:

* Every backend feature
* Full production-scale distributed execution
* Large-scale model training
* Cloud deployment
* Full frontend feature set
* Perfect performance optimization
* Final benchmark numbers
* Every possible integration
* Every technology identified during research

These belong to later implementation phases unless they are required to validate the architecture.

---

## 5. Progressive Definition of Done

Maintain a living definition of done rather than a single permanent checklist.

Use stages such as:

```text
Scaffolding
    ↓
MVP
    ↓
Production Baseline
    ↓
Performance/Scaling
    ↓
Production Hardening
    ↓
Resume/Demo Ready
```

Each stage should define its own acceptance criteria.

Store the current criteria under `agents/spec/` and update them as the architecture evolves.

---

## 6. Required Final Scaffolding Handoff

At the end of scaffolding, produce a concise handoff for future agents containing:

* Current problem definition
* Current architecture
* Repository structure
* Technology stack
* Current implementation status
* What works
* What is stubbed
* Known technical risks
* Highest-priority next steps
* Open architectural questions
* Benchmarking status
* Testing status
* Observability status
* Important decisions and rejected alternatives

The handoff should let a new agent become productive by reading the repository rather than relying on previous conversation context.

---

## 7. Agent Behavior Going Forward

Future agents should follow this loop:

```text
Read context
→ Understand current architecture
→ Identify the smallest useful change
→ Implement
→ Test
→ Benchmark when relevant
→ Review
→ Update documentation/experiments/decisions
→ Commit cleanly
```

Do not allow agents to continuously rewrite architecture without evidence.

Prefer incremental, measurable improvements.

The repository should gradually become more sophisticated through **validated engineering iteration**, not through speculative complexity.

---

## 8. North Star

The goal is not to create the largest robotics project possible.

The goal is to create a project that, when shown to an NVIDIA/Tesla/Amazon Robotics/Google Robotics engineer, demonstrates:

**“This person understands how ML systems become reliable, measurable, scalable robotics infrastructure.”**

Every major implementation decision should contribute toward that signal.
