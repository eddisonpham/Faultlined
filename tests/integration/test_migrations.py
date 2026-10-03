"""Integration tests for the migration runner (ADR 0028), against real PostgreSQL.

Each test builds its own throwaway database instead of using the shared
`<dbname>_test` catalog, for one reason: applying a synthetic migration writes
its version into `schema_migrations`, and a version the shipped registry does
not know would make every later `status()` call on the shared database report
the database as ahead of the code. The ledger has to start empty, so the
database does too. Everything is dropped in the fixture teardown.

What is pinned here, in the order an operator meets it: a fresh database reads
as having nothing applied; `upgrade` applies the real baseline DDL and records
it; a second `upgrade` is a no-op; an injected migration applies exactly once
and only once even when `upgrade` runs again; and a database that knows a
version this code does not is refused, not migrated past.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import Connection
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from data_engine.catalog import migrations
from data_engine.catalog.database import connect, initialize_schema
from data_engine.catalog.migrations import BaselineMigration, Migration
from data_engine.config import Settings
from tests.conftest import postgres_test_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
def fresh_settings() -> Iterator[Settings]:
    """An empty throwaway database, dropped after the test.

    The DSN's credentials are reused; only the database name is new, so nothing
    outside the cluster this test stack already uses is touched.
    """
    base = postgres_test_dsn()
    if not base:
        pytest.skip("no DE_DATABASE_URL configured")
    dbname = f"data_engine_mig_{uuid.uuid4().hex[:8]}"
    info = _conn_info(base)
    with psycopg.connect(**{**info, "dbname": "postgres"}, autocommit=True) as maintenance:
        maintenance.execute(f'CREATE DATABASE "{dbname}"')
    try:
        yield Settings(_env_file=None, database_url=make_conninfo(**{**info, "dbname": dbname}))
    finally:
        with psycopg.connect(**{**info, "dbname": "postgres"}, autocommit=True) as maintenance:
            maintenance.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


class _ProbeMigration:
    """A migration whose body a test can see and reset."""

    version: str
    name: str = "probe"

    def __init__(self, version: str, body: str = "SELECT 1") -> None:
        self.version = version
        self._body = body
        self.calls = 0

    def apply(self, connection: Connection[Any]) -> None:
        self.calls += 1
        connection.execute(self._body)


def _conn_info(dsn: str) -> dict[str, str]:
    return {key: str(value) for key, value in conninfo_to_dict(dsn).items()}


def _table_exists(settings: Settings, table: str) -> bool:
    with connect(settings) as connection:
        row = connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s", (table,)
        ).fetchone()
    return row is not None


# ------------------------------------------------------------------- the ledger


def test_a_fresh_database_starts_with_an_empty_ledger(fresh_settings: Settings) -> None:
    state = migrations.status(fresh_settings)
    assert state["applied"] == []
    assert [m.version for m in migrations.compute_pending([], migrations.REGISTRY)] == [
        m.version for m in migrations.REGISTRY
    ]


def test_upgrade_on_a_fresh_database_applies_and_records_the_baseline(
    fresh_settings: Settings,
) -> None:
    applied = migrations.upgrade(fresh_settings)
    assert applied == ["0001"]
    # The real DDL ran: the catalog tables exist, not just the ledger row.
    assert _table_exists(fresh_settings, "jobs")
    assert _table_exists(fresh_settings, "schema_migrations")
    state = migrations.status(fresh_settings)
    assert state["applied"] == ["0001"]
    assert state["pending"] == []
    assert state["unknown"] == []


def test_upgrade_is_a_noop_when_everything_is_applied(fresh_settings: Settings) -> None:
    initialize_schema(fresh_settings)
    assert migrations.upgrade(fresh_settings) == []


def test_initialize_schema_records_the_baseline_it_built(fresh_settings: Settings) -> None:
    """The safety-net path and the migration ledger must agree."""
    initialize_schema(fresh_settings)
    state = migrations.status(fresh_settings)
    assert state["applied"] == ["0001"]
    assert state["pending"] == []


# ---------------------------------------------------------- once-only semantics


def test_an_injected_migration_applies_exactly_once(fresh_settings: Settings) -> None:
    initialize_schema(fresh_settings)
    probe = _ProbeMigration("9001", "CREATE TABLE probe_done (id integer)")
    registry: tuple[Migration, ...] = (BaselineMigration(), probe)

    # initialize_schema already built and recorded the baseline, so only the
    # probe is pending.
    assert migrations.upgrade(fresh_settings, migrations=registry) == ["9001"]
    assert probe.calls == 1
    assert _table_exists(fresh_settings, "probe_done")

    # The second run skips it: once recorded, a migration never runs again.
    assert migrations.upgrade(fresh_settings, migrations=registry) == []
    assert probe.calls == 1
    state = migrations.status(fresh_settings, migrations=registry)
    assert state["applied"] == ["0001", "9001"]


def test_a_failing_migration_leaves_no_version_behind(fresh_settings: Settings) -> None:
    initialize_schema(fresh_settings)
    broken = _ProbeMigration("9002", "CREATE TABLE this is not sql")
    registry: tuple[Migration, ...] = (BaselineMigration(), broken)
    from psycopg.errors import SyntaxError as PsycopgSyntaxError

    with pytest.raises(PsycopgSyntaxError):
        migrations.upgrade(fresh_settings, migrations=registry)
    # The transaction rolled back: no version row, no partial state to clean up.
    state = migrations.status(fresh_settings)
    assert "9002" not in state["applied"]
    assert broken.calls == 1


# ------------------------------------------------------------- ahead of the code


def test_a_database_newer_than_the_code_is_refused(fresh_settings: Settings) -> None:
    initialize_schema(fresh_settings)
    probe = _ProbeMigration("9004")
    registry: tuple[Migration, ...] = (BaselineMigration(), probe)
    migrations.upgrade(fresh_settings, migrations=registry)

    # The shipped registry does not know 9004; migrating past it would be
    # running old code against a newer catalog.
    with pytest.raises(RuntimeError, match="newer than this code"):
        migrations.upgrade(fresh_settings)

    state = migrations.status(fresh_settings)
    assert state["unknown"] == ["9004"]
