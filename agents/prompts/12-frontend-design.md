# Frontend research and design (run before building the UI)

Use the `frontend-engineer` role. Read `agents/architecture/frontend.md`, `api.md`, `problem.md`, and the current UI scope in `definition-of-done.md`.

## Research (record in `agents/architecture/frontend-design.md`, with URLs and access dates)
Study real engineering/platform UIs and note what makes them credible — layout density, navigation, tables, status semantics,
time-series presentation, log/trace drill-down, empty/error states. Suggested references (verify current state): Grafana, Argo Workflows UI,
Buildkite, GitHub Actions run view, Weights & Biases, MLflow UI, Ray Dashboard, Foxglove, Rerun, Linear (for restraint), Datadog/Honeycomb (for drill-down).
Extract concrete patterns, not vibes.

## Design
1. Information architecture: pages and the operator questions each answers (what's running, what failed and why, what's slow, what changed, what data/model was used).
2. Design principles derived from the research, including explicit anti-goals (no generic admin-template look, no gradients/glass/decorative charts, no marketing hero).
3. Design tokens (type scale, spacing, restrained palette, status colors that survive colorblindness), component inventory, and state coverage (loading/empty/error/stale/partial).
4. Data needs per page mapped to existing API endpoints; propose API additions via ADR, not ad hoc.

## Build
Implement page by page against the real API, smallest first (jobs list → job detail with logs/lineage → resources → experiments/benchmarks → datasets/models → artifacts).
Test critical flows; keep bundle and render performance in check; record any UI benchmarks as experiments.

## Acceptance
Every page has a documented operator question; screenshots of real (not mocked) data states are stored under `agents/architecture/frontend-design/`.
