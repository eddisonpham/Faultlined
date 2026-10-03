"""Versioned forward-only catalog migrations (ADR 0028).

The schema has always been built by `initialize_schema` - idempotent DDL on
every process start. That stays. This module adds what idempotent DDL cannot
express: a recorded version, changes that run once (backfills, renames,
destructive steps), and the ability to say which database is behind or ahead.

Design, in one paragraph: migrations are explicit Python objects in one ordered
tuple (`version`, `name`, `apply(connection)`), not files discovered by
scanning, so mypy sees the registry and nothing runs that nobody imported. Each
applied version is recorded in `schema_migrations` inside the same transaction
as the change, so a crashed runner leaves no version behind unapplied work.
`pg_advisory_xact_lock` serializes concurrent runners (a worker and an operator
both migrating at startup must not race). Forward-only: a rollback is the
restore drill, which is already the procedure this project practices.

The shipped registry contains exactly the baseline. The baseline is the
existing idempotent DDL: `initialize_schema` keeps running on every start and
records `0001` after it succeeds, so a database that has only ever been started
by the code already reads as baseline-applied and `de migrate` runs only real
pending work. Tests inject synthetic migrations through the `migrations=`
parameter to prove once-only semantics without committing a fake migration to
make the mechanism look used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from psycopg import Connection

from data_engine.catalog.database import run_schema_ddl
from data_engine.config import Settings

#: Advisory lock key. A literal constant, not a hash: the value only has to be
#: stable within this project, and a named constant reads better in pg_locks.
MIGRATION_LOCK_KEY = 0x464C544D  # 'FLTM'

_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    name text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


class Migration(Protocol):
    """One forward-only schema step, applied inside a single transaction."""

    # Read-only properties, so a frozen dataclass satisfies the protocol: an
    # implementation that cannot renumber itself is exactly the point.
    @property
    def version(self) -> str: ...

    @property
    def name(self) -> str: ...

    def apply(self, connection: Connection[Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class BaselineMigration:
    """Version 0001: the idempotent DDL that every database already runs."""

    version: str = "0001"
    name: str = "baseline"

    def apply(self, connection: Connection[Any]) -> None:
        run_schema_ddl(connection)


#: The shipped registry. Ordered by version; the runner refuses duplicates and
#: gaps are allowed (a renumbered or withdrawn draft must not reorder history).
REGISTRY: tuple[Migration, ...] = (BaselineMigration(),)


def applied_versions(connection: Connection[Any]) -> list[str]:
    """Versions recorded in `schema_migrations`, oldest first."""
    connection.execute(_TABLE)
    rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [str(row["version"]) for row in rows]


def compute_pending(
    applied: list[str], migrations: tuple[Migration, ...] = REGISTRY
) -> tuple[Migration, ...]:
    """Migrations not yet recorded, in registry order.

    Pure so the ordering rules are testable without a database: registry order
    is authoritative (gaps allowed), an applied version skips its migration,
    and duplicate versions in the registry are a programming error caught here
    rather than a double-apply at 3am.
    """
    seen: set[str] = set()
    for migration in migrations:
        if migration.version in seen:
            raise ValueError(f"duplicate migration version {migration.version!r}")
        seen.add(migration.version)
    done = set(applied)
    return tuple(migration for migration in migrations if migration.version not in done)


def unknown_versions(applied: list[str], migrations: tuple[Migration, ...] = REGISTRY) -> list[str]:
    """Recorded versions the registry does not know.

    A non-empty result means the database is newer than the code running
    against it - a downgraded worker, or a restore into the wrong checkout.
    `upgrade` refuses in that state and `de migrate --status` exits nonzero.
    """
    known = {migration.version for migration in migrations}
    return [version for version in applied if version not in known]


def status(settings: Settings, migrations: tuple[Migration, ...] = REGISTRY) -> dict[str, Any]:
    """Applied, pending, and ahead-of-code versions for one database."""
    from data_engine.catalog.database import connect

    with connect(settings) as connection:
        applied = applied_versions(connection)
    return {
        "applied": applied,
        "pending": [migration.version for migration in compute_pending(applied, migrations)],
        "unknown": unknown_versions(applied, migrations),
    }


def upgrade(
    settings: Settings,
    *,
    migrations: tuple[Migration, ...] = REGISTRY,
) -> list[str]:
    """Apply every pending migration, each in one transaction, in registry order.

    Returns the versions applied by this call. Running it twice is a no-op the
    second time: applied versions are skipped, so the function is safe to call
    from a startup path as well as `de migrate`.
    """
    from data_engine.catalog.database import connect

    applied_by_this_call: list[str] = []
    with connect(settings) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        applied = applied_versions(connection)
        unknown = unknown_versions(applied, migrations)
        if unknown:
            raise RuntimeError(
                "database is newer than this code: recorded versions "
                + ", ".join(unknown)
                + " are not in the registry; refusing to migrate"
            )
        for migration in compute_pending(applied, migrations):
            # One transaction per migration: `connect` yields an autocommit-off
            # connection, so the commit below covers the change and its version
            # row together.
            migration.apply(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
            connection.commit()
            applied_by_this_call.append(migration.version)
    return applied_by_this_call
