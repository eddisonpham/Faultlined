from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from data_engine.catalog.database import initialize_schema


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
