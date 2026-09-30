# agents/ — persistent project context

Project knowledge that outlives a session. If it is not here, it is not known.

| Directory | Holds | State |
|---|---|---|
| [spec/](spec/) | Problem, requirements, environment, original specs, definition of done | Current; MVP stage closed |
| [research/](research/) | Source log, industry patterns, technology matrix | Written in phases 01-02 |
| [architecture/](architecture/) | Overview, components, data flow, API, storage, repo layout, decision matrix | Written; matches the shipped code |
| [implementation/](implementation/) | Coding standards, implementation status, slice plans | Current |
| [testing/](testing/) | Testing standards, failure-mode catalog | Catalog complete, no row left `planned` |
| [benchmarking/](benchmarking/) | Methodology, metric schema, backlog | One baseline committed, more need owner approval |
| [observability/](observability/) | Logging, metrics, tracing conventions | JSON logs, correlation ids, runtime metrics |
| [experiments/](experiments/) | Experiment records and the registry | EXP-0001 to EXP-0007 |
| [reviews/](reviews/) | Engineering reviews and assessments | Three reviews, findings closed |
| [decisions/](decisions/) | Architecture decision records | ADR 0001 to ADR 0025 |
| [roles/](roles/) | Agent role definitions | Seeded |
| [prompts/](prompts/) | Phase prompts, run in order | Phases 01-03 used, 04-07 remaining |
| [HANDOFF.md](HANDOFF.md) | Current state, risks, next steps | Read this first |

## Conventions

- Markdown only. Update a doc in the same commit as the change it describes.
- Describe the current state. History belongs in ADRs, experiment records and git.
- Link to other docs instead of copying content.
- Mark unfinished sections `TODO(phase NN)` so they stay greppable.
- Table over prose, one fact per row or bullet.