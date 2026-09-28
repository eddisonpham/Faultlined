# Repository Layout

Where every kind of code belongs. Component ↔ directory mapping must stay 1:1 with
[components.md](components.md) and [../implementation/status.md](../implementation/status.md).

## Tree (current state after phase 04; phases 05+ fill business logic)

```text
.
├── CLAUDE.md / README.md
├── agents/                      # persistent project context (docs, ADRs, specs) — never application code
├── pyproject.toml               # uv-managed project, tools, markers, coverage gate
├── uv.lock                      # committed dependency lock
├── .python-version              # Python 3.14 selection for uv
├── justfile                     # one entry point for setup/fmt/lint/typecheck/test/bench/run
├── src/data_engine/
│   ├── config.py                # environment-based settings
│   ├── cli.py                   # CLI entry points (component 12)
│   ├── api/                     # component 1: FastAPI app, routers, schemas
│   │   └── app.py
│   ├── catalog/                 # component 2: repositories, models, migrations
│   │   ├── repository.py  migrations/
│   ├── jobs/                    # components 3–4: queue, scheduler, worker, lifecycle
│   │   ├── queue.py  contracts.py  state.py
│   ├── ingest/                  # component 5: episode ingest service + format readers
│   │   ├── service.py            # synthetic JSON vertical slice; future format registration
│   │   └── readers/              # base.py; MCAP/LeRobot implementations follow
│   ├── validation/              # component 6: profile engine, rules, quarantine
│   │   └── rules/
│   ├── indexing/                # component 7: metadata/stats extraction, Parquet export
│   ├── builds/                  # component 8: selection, planner, exporter, manifests
│   ├── workloads/               # component 9: workload SDK + runner + registry
│   │   └── sdk.py
│   ├── storage/                 # component 10: content-addressed artifact store
│   │   └── artifacts.py
│   └── observability/           # component 11: logging context, telemetry, metrics, reason codes
│       ├── logging.py  telemetry.py  metrics.py  reason_codes.py
├── frontend/                    # component 13 (phase 12); consumes /api/v1 only
├── benchmarks/                  # component 14: harness + micro-benchmarks + baselines/
│   ├── harness.py  baselines/
├── tests/
│   ├── unit/  integration/  contract/  e2e/
│   └── fixtures/                # tiny real-format fixtures — the only committed data
├── docs/api/                    # committed OpenAPI contract (phase 05)
├── scripts/                     # repository tooling (check_repo_hygiene.py)
├── .env.example                 # safe placeholders; local .env is gitignored
├── .github/workflows/           # CI (fmt, lint, types, tests, coverage, hygiene)
└── .pre-commit-config.yaml      # pre-commit hooks (secrets, ruff, mypy, hygiene)
```

Naming: the product is **Faultlined**, a robot episode data engine. The distribution is `faultlined`, the import package is
`data_engine`, and the CLI is `de`. Runtime state (`var/`, `.env`) is gitignored. Host-based local development and
optional containers are recorded in [ADR 0012](../decisions/0012-host-based-development.md).

## Dependency direction rules

```text
api / cli ──► jobs, builds, ingest, validation, indexing, workloads, catalog, storage, observability
jobs ──► stage handlers (ingest, validation, indexing, builds, workloads)   [via handler registry]
stage handlers ──► catalog, storage, observability
workloads (user/model code) ──► workloads/sdk ONLY
catalog / storage / observability ──► (stdlib + drivers; no upward imports)
```

- **No cycles.** No import-linter dependency added in phase 04: the package is a small modular monolith with no feature
  modules yet. Enforce boundaries in review/tests; add a lightweight import check only when modules exist to violate them.
- `api/` contains no business logic; `catalog/` contains no orchestration; `jobs/` knows nothing about formats.
- Readers/plugins extend via registries (`ingest/readers/`, `validation/rules/`, `workloads/registry.py`) — adding a
  format/rule/workload touches its own module + tests only ([api.md](api.md) internal interfaces).

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
