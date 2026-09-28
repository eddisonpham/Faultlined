# agents/ — persistent project context

Everything an agent needs to work on this project lives here. If it isn't here, it doesn't exist.

| Directory / file | Contents | Status |
|---|---|---|
| [spec/](spec/) | Original specs (read-only), living definition of done, problem, requirements, environment | problem + environment filled (phase 01); requirements in phase 02 |
| [research/](research/) | Source log, industry patterns, technology matrix | filled (phase 01) |
| [architecture/](architecture/) | Overview, components, data flow, APIs, storage, compute, failure handling, repo layout, decision matrix | filled in phases 02–03 |
| [implementation/](implementation/) | Coding standards, implementation status, vertical-slice definition | standards seeded |
| [testing/](testing/) | Testing standards and failure-mode catalog | standards seeded |
| [benchmarking/](benchmarking/) | Methodology (definitive), metric schema, backlog | methodology seeded |
| [observability/](observability/) | Logging/metrics/tracing conventions | conventions seeded |
| [experiments/](experiments/) | Experiment records + registry (current / tested / rejected / deferred) | template seeded |
| [reviews/](reviews/) | Engineering reviews | template seeded |
| [decisions/](decisions/) | ADRs — canonical record of architectural decisions | ADR 0001–0002 seeded |
| [roles/](roles/) | Specialized agent role definitions | seeded |
| [prompts/](prompts/) | Phase prompts, run in order | seeded |
| [HANDOFF.md](HANDOFF.md) | Current status for the next agent | stub until phase 07 |

## Conventions

- All docs are Markdown. Update docs in the same commit as the change they describe.
- Docs describe the **current** state. History belongs in ADRs, experiment records, and git.
- Link to other docs instead of copying content.
- Mark unfinished sections `TODO(phase NN)` so they are greppable.
