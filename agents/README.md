# agents/ — persistent project context

Everything an agent needs to work on this project lives here. If it isn't here, it doesn't exist.

| Directory / file | Contents | Status |
|---|---|---|
| [spec/](spec/) | Original specs (read-only), living definition of done, problem, requirements, environment | problem (01), requirements (02), environment (01) filled |
| [research/](research/) | Source log, industry patterns, technology matrix, problem candidates | filled (phase 01–02) |
| [architecture/](architecture/) | Overview, components, data flow, APIs, storage, compute, failure handling, repo layout, decision matrix | written in phases 02–03 |
| [implementation/](implementation/) | Coding standards, implementation status, vertical-slice definition | tooling + synthetic ingest slice implemented; phase 05/06 work uncommitted; review follow-ups open |
| [testing/](testing/) | Testing standards and failure-mode catalog | standards + failure-mode plan; slice-related failure tests added (phase 05) |
| [benchmarking/](benchmarking/) | Methodology (definitive), metric schema, backlog | schema/harness foundations implemented; no measured baseline; backlog populated |
| [observability/](observability/) | Logging/metrics/tracing conventions | JSON logging, correlation, telemetry and metric primitives; runtime instrumentation incomplete |
| [experiments/](experiments/) | Experiment records + registry (current / tested / rejected / deferred) | EXP-0001 planned only; no measured experiments |
| [reviews/](reviews/) | Engineering reviews | 2026-09-28 scaffolding review rejects stage acceptance; follow-ups open |
| [decisions/](decisions/) | ADRs — canonical record of architectural decisions | ADRs 0001–0013 accepted |
| [roles/](roles/) | Specialized agent role definitions | seeded |
| [prompts/](prompts/) | Phase prompts, run in order | seeded |
| [HANDOFF.md](HANDOFF.md) | Current status for the next agent | current implementation, review findings, blockers, and next work |

## Conventions

- All docs are Markdown. Update docs in the same commit as the change they describe.
- Docs describe the **current** state. History belongs in ADRs, experiment records, and git.
- Link to other docs instead of copying content.
- Mark unfinished sections `TODO(phase NN)` so they are greppable.
