from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from data_engine.catalog import database
from data_engine.catalog.database import initialize_schema
from data_engine.config import Settings


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


@pytest.mark.unit
def test_initialize_schema_executes_each_nonempty_statement() -> None:
    connection = FakeConnection()

    @contextmanager
    def fake_connect(_settings: Any = None) -> Iterator[FakeConnection]:
        yield connection

    with patch("data_engine.catalog.database.connect", fake_connect):
        initialize_schema()

    assert len(connection.statements) == 5
    assert all(statement.strip() for statement in connection.statements)
    assert "CREATE TABLE IF NOT EXISTS jobs" in connection.statements[0]


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
    assert captured["dsn"] == "postgresql://user@127.0.0.1:5432/db"
