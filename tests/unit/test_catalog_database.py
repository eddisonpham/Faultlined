from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from data_engine.catalog import database
from data_engine.catalog.database import initialize_schema
from data_engine.config import Settings
from data_engine.jobs.state import DEFAULT_MAX_ATTEMPTS


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[object] = []
        self.commits = 0

    def execute(self, statement: str, params: object = None) -> None:
        self.statements.append(statement)
        self.params.append(params)

    def commit(self) -> None:
        self.commits += 1


@pytest.mark.unit
def test_initialize_schema_executes_each_nonempty_statement() -> None:
    connection = FakeConnection()

    @contextmanager
    def fake_connect(_settings: Any = None) -> Iterator[FakeConnection]:
        yield connection

    with patch("data_engine.catalog.database.connect", fake_connect):
        initialize_schema()

    expected = sum(
        len([s for s in script.split(";") if s.strip()])
        for script in (database._SCHEMA, database._MIGRATIONS)
    )
    # The DDL statements plus the baseline ledger row (ADR 0028).
    assert len(connection.statements) == expected + 1
    assert all(statement.strip() for statement in connection.statements)
    assert "CREATE TABLE IF NOT EXISTS jobs" in connection.statements[0]
    assert "INSERT INTO schema_migrations" in connection.statements[-1]
    assert connection.params[-1] == (database.BASELINE_VERSION, database.BASELINE_NAME)
    assert connection.commits == 1


@pytest.mark.unit
def test_initialize_schema_adds_lifecycle_columns_to_pre_existing_tables() -> None:
    """CREATE TABLE IF NOT EXISTS never adds columns, so migrations must be separate."""
    connection = FakeConnection()

    @contextmanager
    def fake_connect(_settings: Any = None) -> Iterator[FakeConnection]:
        yield connection

    with patch("data_engine.catalog.database.connect", fake_connect):
        initialize_schema()

    migrations = [s for s in connection.statements if s.lstrip().startswith("ALTER TABLE jobs")]
    columns = " ".join(migrations)
    for column in ("attempts", "max_attempts", "deadline_at", "worker_id", "lease_expires_at"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in columns
    assert all("IF NOT EXISTS" in statement for statement in migrations)


@pytest.mark.unit
def test_migration_default_matches_the_retry_budget_constant() -> None:
    """Two sources of truth for the retry budget would silently disagree."""
    assert f"DEFAULT {DEFAULT_MAX_ATTEMPTS};" in database._MIGRATIONS


@pytest.mark.unit
def test_connect_bounds_the_connect_phase() -> None:
    """A stale DSN must fail fast; libpq otherwise retries for minutes."""
    captured: dict[str, Any] = {}

    class FakeCursor:
        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    class FakePsycopgConnect(FakeCursor):
        def __call__(self, dsn: str, **kwargs: Any) -> FakeCursor:
            captured["dsn"] = dsn
            captured.update(kwargs)
            return self

    settings = Settings(database_url="postgresql://user@127.0.0.1:5432/db", _env_file=None)
    fake = patch.object(database.psycopg, "connect", FakePsycopgConnect())
    with fake, database.connect(settings):
        pass

    assert captured["connect_timeout"] == database.CONNECT_TIMEOUT_SECONDS
    assert captured["options"] == f"-c statement_timeout={database.STATEMENT_TIMEOUT_MILLISECONDS}"
    assert captured["dsn"] == "postgresql://user@127.0.0.1:5432/db"
