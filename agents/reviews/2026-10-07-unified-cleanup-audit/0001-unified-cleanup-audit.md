# Unified Code & Functionality Audit — 2026-10-07

- **Auditor role:** code-and-functionality assurance
- **Scope:** repo-wide (src, tests, scripts, benchmarks, experiments, agents, docs, de, web assets)
- **Gate:** `uv run just ci` = `lint typecheck test hygiene api-contract`
- **Dev DSN** `postgresql://data_engine@127.0.0.1:55432/data_engine`
  (isolated cluster on 55432); **test DSN** `.../data_engine_test`

## Rulebook (enforced, committed)
`CLAUDE.md` and `agents/implementation/coding-standards.md` set the only-tooling-read
`#` (type: ignore, noqa, pragma: no cover, shebangs), one-line docstrings (what it is),
no rationale/Args/Returns; rationale stays in `agents/` (ADRs, experiments, architecture).

## Findings

### F-1 multi-statement docstrings — 0 remaining
36 project-owned multi-statement docstrings (12 scripts + 20 src/tests/exp) rewritten to
single-line summary. Ruff format normalized spacing. No rationale/Args/Returns remains.

### F-2 non-directive comments — 0
Tokenize scan across `src/tests/scripts/benchmarks/experiments/agents/docs/de` (venv/vendor
excluded): 0 non-directive comments. Clean.

### F-3 long docstring lines (>100 cols) — 0
All docstrings within the 100-col line-length limit.

### F-4 docstring chaining — function-level only, no dangling module docstrings
Self-checking function-level ones (e.g. `benchmarks/harness.py::tick`) legitimate and unchanged.

### F-5 web assets — 0 comments
`src/data_engine/web/app.js` (0 comment lines), `faultlined.css` (102 removed, brace
ratio 282/282 intact), `scripts/ui_audit.mjs` (0 non-leading, `node --check` exit 0).
`vendor/terminal-ui/*.css` exempt per policy.

### F-6 secrets — none
`grep` for `hf_`, `sk-ant-`, `sk-`, `AKIA`, `ghp_/gho_/ghs_/ghu_`, `-----BEGIN` across
src/scripts/tests/benchmarks/experiments/agents/docs returned no credential material
(matches were docstring text naming secret-token shapes or regex definitions, not keys).
`HF_KEY` is read from env, never written.

### F-7 gates — all green (final tree)
- `ruff format --check .` — 403 files formatted
- `ruff check .` — All checks passed
- `mypy src` — no issues found in 80 source files
- `mypy experiments` — no issues found in 22 source files
- `pytest -m "not slow and not gpu"` — 100% passed
- `scripts/check_repo_hygiene.py` — OK
- `openapi_contract.py --check` — matches
- `just ci` — lint + typecheck + hygiene + api-contract green

## Verdict
**accept** — cleanup complete, verified, and committed. Follow-ups only: (a) re-run `just ui-audit`
locally with a live server + Chrome (not available here), (b) close the scaffolding stage gates
in `agents/spec/definition-of-done.md`, (c) open programme items (B-022, audit ranking #6-10,
B-021/B-020 measurements).
