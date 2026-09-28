# Repository Layout

Where every kind of code belongs. Component ↔ directory mapping must stay 1:1 with
[components.md](components.md) and [../implementation/status.md](../implementation/status.md).

## Tree (target state; phases 04+ create the code)

```text
.
├── CLAUDE.md / README.md
├── agents/                      # all persistent project context (docs, ADRs, specs) — never code
├── pyproject.toml               # uv-managed; single package below
├── src/data_engine/
│   ├── config.py                # env-based settings (deployment.md)
│   ├── cli.py                   # CLI entry points (component 12)
│   ├── api/                     # component 1: FastAPI app, routers, schemas (Pydantic = contract)
│   │   ├── app.py  routers/  schemas/  errors.py
│   ├── catalog/                 # component 2: repositories, models, migrations
│   │   ├── models.py  repositories/  migrations/
│   ├── jobs/                    # components 3–4: queue, scheduler, worker, lifecycle
│   │   ├── queue.py  scheduler.py  worker.py  state.py  retry.py
│   ├── ingest/                  # component 5: readers + registration
│   │   ├── service.py  readers/{mcap.py,lerobot.py,base.py}
│   ├── validation/              # component 6: profile engine, rules, reason codes
│   │   ├── profiles.py  rules/  quarantine.py
│   ├── indexing/                # component 7: metadata/stats extraction, parquet export
│   ├── builds/                  # component 8: selection queries, planner, exporter, manifests
│   │   ├── selection.py  planner.py  export_lerobot.py  manifest.py
│   ├── workloads/               # component 9: workload SDK + runner + registry
│   │   ├── sdk.py  runner.py  registry.py
│   ├── storage/                 # component 10: content-addressed artifact store
│   │   ├── artifacts.py  layout.py  gc.py
│   └── observability/           # component 11: logging context, telemetry, metrics
│       ├── logging.py  telemetry.py  metrics.py  reason_codes.py
├── frontend/                    # component 13 (phase 12); consumes /api/v1 only
├── benchmarks/                  # component 14: harness + micro-benchmarks + baselines/
│   ├── harness.py  bench_ingest.py  bench_validate.py  bench_query.py  bench_build.py
├── tests/
│   ├── unit/  integration/  contract/  e2e/
│   └── fixtures/                # tiny real-format fixtures (MCAP/LeRobot) — the only data committed
├── docs/api/openapi.json        # committed API contract (drift-checked in CI)
├── scripts/                     # repo tooling (check_repo_hygiene.py; one-command wrappers)
└── .github/workflows/           # CI (fmt, lint, types, tests, coverage, hygiene, bench-regression)
```

Naming: package `data_engine` (the product: "robot episode data engine"); CLI `de`. Runtime state (`var/`, `.env`)
is gitignored ([../spec/environment.md](../spec/environment.md) constraints apply to all tooling).

## Dependency direction rules

```text
api / cli ──► jobs, builds, ingest, validation, indexing, workloads, catalog, storage, observability
jobs ──► stage handlers (ingest, validation, indexing, builds, workloads)   [via handler registry]
stage handlers ──► catalog, storage, observability
workloads (user/model code) ──► workloads/sdk ONLY
catalog / storage / observability ──► (stdlib + drivers; no upward imports)
```

- **No cycles.** Enforced by an import-linter check in CI (or a lightweight test if the tool proves heavy — decide at
  phase 04, record in implementation docs).
- `api/` contains no business logic; `catalog/` contains no orchestration; `jobs/` knows nothing about formats.
- Readers/plugins extend via registries (`ingest/readers/`, `validation/rules/`, `workloads/registry.py`) — adding a
  format/rule/workload touches its own module + tests only (api.md internal interfaces).

## Where new code belongs (cheat sheet)

| You are adding… | Goes in… | Tests in… |
|---|---|---|
| An endpoint | `api/routers/` + `api/schemas/` | `tests/contract/` |
| A pipeline stage behavior | its component dir (`ingest/`, `validation/`, …) | `tests/unit/` + `tests/integration/` |
| A new file format | `ingest/readers/<fmt>.py` | `tests/unit/readers/` + fixture in `tests/fixtures/` |
| A validation rule | `validation/rules/<rule>.py` | `tests/unit/rules/` |
| A model/workload | `workloads/` (registered) — never inside engine internals | `tests/e2e/` or `benchmarks/` |
| A metric | `observability/metrics.py` + naming per metric-schema | asserted in stage tests |
| A benchmark | `benchmarks/bench_<area>.py` | self-checking harness |
| A schema change | `catalog/migrations/` + models + storage.md update | migration test |
| UI page/panel | `frontend/` (phase 12) | UI tests colocated |
