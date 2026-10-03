# EXP-0008: Single-episode workflow smoke and operator UI audit

- **Date (UTC):** 2026-10-01
- **Author/agent:** Buffy
- **Status:** done (exploratory smoke; not a benchmark or production baseline)
- **Related ADR / requirement:** ADR 0027; Production Baseline stage criteria

## Hypothesis / purpose

The new task-string triage and navigation can coexist with the existing real LeRobot ingest path, and the Clusters UI remains usable under the project's four supported themes. A bounded smoke should establish workflow wiring only, not scale or quality claims.

## Change under test

One episode from the locally available v3.0 LeRobot tabular subset was submitted through the actual Postgres queue and worker and validated with a matching profile. A saved `state=valid` slice was read against the shared persistent `_test` catalog, but its bounded manifest did not show that run's episode. Build/export was not attempted. The browser audit used a temporary API process targeting `data_engine_test` and performed only read requests. An initial server launch briefly targeted the development DB; the CLI calls idempotent schema initialization before serving. That process was stopped before any browser requests were sent, and no job/episode/operator records were submitted there. No DB reset was run; local artifacts stayed under ignored `var/` paths.

## Configuration

- Workload: `ui-workflow-smoke-v1`, one real local episode (`episode_index=7`), one ingest/validation run; no warmups or repeats.
- Database: unique run-keyed ingest/validation writes to `data_engine_test` on local PostgreSQL port 55432. Browser audit server was bound to a custom API port with the same test DB; it was stopped afterward.
- Artifact root: `var/experiments/workflow-artifacts`. No export was produced.
- UI: Node v24.18.0 + local headless Chrome; 13 route entries × 4 themes, each loaded under a forced theme swap.
- Data: local `lerobot-svla-so101`, LeRobot v3.0; `meta/info.json` declares 50 episodes / 11,939 frames / 1 task. The selected indexed episode has 203 frames; no camera video files are present locally. Data shard SHA-256: `579ad57e2454359fa9f2c0e83525991bd2e0305b7b2914d0b1181da3c6ad9949` (369,943 bytes). Episode-index shard SHA-256: `191998bffd2680c477a4000270cf943bc423ba5fb54ee8b8244b57db062c5209` (72,560 bytes). Metadata SHA-256: `254909942a6cbfb4692a239e4d0aa5c68ec8eb16f81b6593985ea7f2bb2823e3` (3,401 bytes).

## Provenance

- Repository commit: not recorded; worktree is dirty and includes prior user changes.
- Platform: Windows host, Python 3.14.5, uv-locked environment, local PostgreSQL; no GPU use in this CPU/data path.
- Source revision: locally cached fixture; upstream revision/commit is not recorded in the fixture cache, so the file hashes above identify the bytes used, not a Hub commit.
- Seed: random 12-hex-character run key used only for idempotency and private test labels. No separate workflow diagnostic JSONL was written.
- Background load: not controlled or measured.

## Results

| Check | Result |
|---|---|
| Source ingest | Passed; `episode_index=7`, 203 frames, job succeeded; artifact hash matched source-shard hash; `produced_by` lineage verified |
| Validation | Passed; required action and observation.state, minimum 100 frames; episode state became `valid` |
| Slice | Created with `state=valid`; manifest read was capped at 100 while the shared `_test` DB already held 1,294 unrelated episodes. It did not include the smoke episode in the returned 100. No membership or slice-latency claim is made |
| Build/export | Not attempted; no result is claimed |
| UI | Browser audit clean: 52 page loads across all four themes. First pass exposed false positives: Chrome reported SVG SMIL animations as generic `css` animations; the audit now checks only explicit named app CSS animations. Repeat passed 52/52 |
| Focused cluster checks | 45 passed; all 15 cluster SQL integration tests passed against the `_test` DB |
| Repository gate | `just ci`: 1,279 passed, 91.25% coverage, strict mypy (75 source + 22 experiment files), Ruff/format, hygiene, and OpenAPI contract all passed. One Starlette/httpx TestClient deprecation warning remains |

Only one indexed 203-frame episode was exercised, so the small, medium, and large workflow campaigns are unexecuted. The source manifest's declared totals do not establish that those records or bytes exist in the local fixture. This is an end-to-end wiring smoke, not a scale trial or a production benchmark.

## Trade-offs

The persistent test catalog contains unrelated state and old rows; it was not reset. This makes a bounded 100-item slice read incomplete for this scenario and prevents interpreting it as full-catalog coverage. One trial on an uncontrolled machine is not statistically meaningful. The browser audit measures computed visual failure conditions, not operator comprehension, no-JS task completion, keyboard traversal end to end, screenshots at target viewports, or a screen-reader session.

The first UI audit identified a diagnostic bug in the script's interpretation of generic CSS/SVG animation objects; narrowing the check to explicit CSS animation names removed false positives without expanding the allowlist. A separate unit assertion that all pages with no lineage graph contain no SVG was narrowed to test for the graph-specific class, because the robot wordmark intentionally uses inline SVG.

## Conclusion

Decision: **defer** any production-baseline or scale claim. The data-backed ingest/validation smoke and all-theme browser audit pass. The initial API process startup may have run idempotent schema DDL against the development database, but no application requests or record submissions were made there; the process was stopped before the audit. A publishable small-tier campaign needs an authorized clean isolated catalog or a fresh bounded per-run test database, enough actual source episodes, at least five end-to-end trials, and complete provenance. Medium/large targets remain unexecuted.

## Follow-ups

- Keep Stage 3 open; this experiment is exploratory evidence only.
- Use a dedicated per-campaign database/artifact root with documented exact-ID cleanup before the next full workflow.
- Capture desktop and narrow viewport screenshots for all themes and run a keyboard/screen-reader review; the current browser audit checks computed styles only.
- Build/export and scale curves remain unverified.
- Reconcile stale stage/status statements in `CLAUDE.md` and `agents/README.md` as a separate documentation change.
