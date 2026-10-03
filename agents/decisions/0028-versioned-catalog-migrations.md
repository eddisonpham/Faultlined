# ADR 0028: Versioned forward-only catalog migrations

- **Date:** 2026-10-02
- **Status:** accepted
- **Stage:** 5 (Production Hardening) — closes [HANDOFF §7](../HANDOFF.md) item 7 and the
  deployment tier's "`de migrate` does not exist" row
- **Related:** [ADR 0005](0005-catalog-and-job-queue.md) (PostgreSQL catalog),
  [deployment.md](../architecture/deployment.md) (production-like local tier),
  [backup-and-restore.md](../../docs/runbooks/backup-and-restore.md) (restore drills against a
  schema that must match what the code writes)

## Context

The catalog schema is created and evolved by `initialize_schema`, which runs two scripts on
every process start: an idempotent `CREATE TABLE IF NOT EXISTS` baseline and an idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` block. Both are idempotent, so every start is a
no-op on a current database and a catch-up on an older one. That mechanism has carried the
project from the vertical slice through stage 3, and it has real virtues: no operator step,
no drift between dev and test, and a restore drill that can rebuild a schema from code alone.

It has three structural limits, all of which are ahead of the project rather than behind it:

1. **No version record.** Nothing in the database says which changes it has received. A
   database's schema state is only knowable by diffing `information_schema` against the code.
2. **Idempotence is a ceiling, not a floor.** `ADD COLUMN IF NOT EXISTS` cannot express a
   backfill, a data migration, a rename with a cutover, or a destructive step — the classes of
   change a catalog accumulates once real datasets live in it.
3. **No way to know a database is *newer* than the code.** A downgraded worker against an
   upgraded catalog is exactly the failure class a restore drill is supposed to catch, and the
   current mechanism cannot even name it.

The empty `src/data_engine/catalog/migrations/` directory has existed since scaffolding as the
designated home for the mechanism.

## Decision

1. **A versioned, forward-only migration runner lives in `data_engine.catalog.migrations`.**
   Migrations are explicit Python modules registered in an ordered tuple — `version`, `name`,
   `apply(connection)` — not files discovered by scanning. Each migration runs inside one
   transaction and records itself in a `schema_migrations` table
   (`version text PRIMARY KEY, name text, applied_at timestamptz`). An advisory lock
   (`pg_advisory_xact_lock`) serializes concurrent runners, so two workers starting on the same
   database cannot race.

2. **`initialize_schema` stays, and becomes migration 0001.** The existing idempotent DDL is
   not history to reconstruct; it is the baseline. `initialize_schema` keeps running on every
   process start (the safety net that made dev, test and restore-drill databases all work with
   no operator step), and it records `0001 baseline` in `schema_migrations` after the DDL
   succeeds. A database that has only ever seen `initialize_schema` therefore already reads as
   baseline-applied, and `de migrate` runs only genuine pending work.

3. **`de migrate` exists, with `--status`.** Status prints applied and pending versions and
   exits nonzero when the database is *newer* than the code (a version recorded that the
   registry does not know), because running old code against a newer catalog is a hard error,
   not a warning to scroll past.

4. **Forward-only.** No down migrations. A rollback is a restore from backup
   ([backup-and-restore.md](../../docs/runbooks/backup-and-restore.md)), which is the procedure
   the project already drills. Writing untested down-scripts for a change no one will run them
   against is confidence theater.

5. **The shipped registry ships exactly one migration — the baseline.** No synthetic migration
   is committed to make the mechanism look used. The once-only, transactional, recorded
   semantics are proven by tests that inject migrations into the runner's API, which is
   parameterized for exactly that reason.

## Alternatives considered

- **Keep inline idempotent DDL only** (status quo). Rejected: limits 1–3 above are stage-5
  concerns (restore drills, prod-like tier, future schema work), and the migrations directory
  already reserved the design space. Cost of the runner is small; cost of retrofitting version
  history after a real backfill ships is not.
- **Alembic.** Rejected for the same reasons as in [ADR 0005](0005-catalog-and-job-queue.md):
  a new dependency, its own revision-file dialect, and autogenerate against a schema the
  project prefers to state in code. The runner is ~150 lines with no dependencies.
- **Raw SQL files** discovered from a directory. Rejected: the project's schema is Python
  constants today; file scanning adds a discovery mechanism that mypy cannot check.

## Consequences

- `deployment.md`'s "What is manual" table moves `de migrate` from "does not exist" to "exists;
  running it before starting upgraded code is the operator step".
- Fresh installs need no change: `initialize_schema` still builds everything, including
  `schema_migrations`.
- Every future schema change that is not expressible as idempotent DDL **must** land as a
  migration in the registry, with the baseline kept in sync for fresh installs.
- The restore drill gains a cheap assertion: `de migrate --status` against a restored dump
  must report no pending and no unknown versions.
