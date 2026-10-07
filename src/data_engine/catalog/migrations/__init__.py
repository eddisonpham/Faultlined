"""Versioned forward-only catalog migrations (ADR 0028)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from psycopg import Connection

from data_engine.catalog.database import run_schema_ddl
from data_engine.catalog.migrations.task_vocabulary import TaskVocabularyMigration
from data_engine.config import Settings

MIGRATION_LOCK_KEY = 0x464C544D

_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    name text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


class Migration(Protocol):
    """One forward-only schema step, applied inside a single transaction."""

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


REGISTRY: tuple[Migration, ...] = (BaselineMigration(), TaskVocabularyMigration())


def applied_versions(connection: Connection[Any]) -> list[str]:
    """Versions recorded in `schema_migrations`, oldest first."""
    connection.execute(_TABLE)
    rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [str(row["version"]) for row in rows]


def compute_pending(
    applied: list[str], migrations: tuple[Migration, ...] = REGISTRY
) -> tuple[Migration, ...]:
    """Migrations not yet recorded, in registry order."""
    seen: set[str] = set()
    for migration in migrations:
        if migration.version in seen:
            raise ValueError(f"duplicate migration version {migration.version!r}")
        seen.add(migration.version)
    done = set(applied)
    return tuple(migration for migration in migrations if migration.version not in done)


def unknown_versions(applied: list[str], migrations: tuple[Migration, ...] = REGISTRY) -> list[str]:
    """Recorded versions the registry does not know."""
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
    """Apply every pending migration, each in one transaction, in registry order."""
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
            migration.apply(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (migration.version, migration.name),
            )
            connection.commit()
            applied_by_this_call.append(migration.version)
    return applied_by_this_call
