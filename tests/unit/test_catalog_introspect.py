"""Live catalog introspection (ADR 0024).

The page built on this claims it cannot drift from the database, because it asks
the database. These tests cover the half of that claim which is ours: turning
`pg_catalog` rows into the model the page renders. The SQL itself is PostgreSQL's
job, and the end-to-end run checks the rest.

The failure cases carry more weight than the happy path here. A schema page that
500s when the catalog is down is worse than no schema page, and a page that
draws a table it could not count is quietly lying about what is in here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from data_engine.catalog.introspect import describe

pytestmark = pytest.mark.unit


class Scripted:
    """A connection that answers introspection queries from a fixed script."""

    def __init__(self, results: list[Any]) -> None:
        self.results = iter(results)
        self.statements: list[str] = []
        self.current: Any = None

    def execute(self, query: str, params: Any = None) -> Scripted:
        self.statements.append(query)
        self.current = next(self.results, None)
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self.current if isinstance(self.current, dict) else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.current if isinstance(self.current, list) else []


@contextmanager
def _connection(connection: Scripted) -> Iterator[Scripted]:
    yield connection


def _column(name: str, position: int, *, table: str = "episodes", **over: Any) -> dict[str, Any]:
    row = {
        "table_name": table,
        "column_name": name,
        "ordinal_position": position,
        "data_type": "text",
        "is_nullable": "NO",
        "column_default": None,
        "is_primary": False,
    }
    row.update(over)
    return row


def _scripted(results: list[Any]) -> Any:
    connection = Scripted(results)
    return patch("data_engine.catalog.introspect.connect", return_value=_connection(connection))


def test_the_model_groups_columns_under_their_table() -> None:
    with _scripted(
        [
            [_column("id", 1, data_type="uuid", is_primary=True), _column("state", 2)],
            [
                {
                    "source": "jobs",
                    "source_column": "job_id",
                    "target": "builds",
                    "target_column": "id",
                }
            ],
            {"n": 7},
        ]
    ):
        model = describe()

    assert model["reachable"] is True
    assert model["table_count"] == 1
    assert model["column_count"] == 2
    assert [c["name"] for c in model["tables"][0]["columns"]] == ["id", "state"]
    assert model["tables"][0]["row_count"] == 7


def test_a_referenced_table_is_listed_under_referenced_by() -> None:
    """`builds` is referenced *by* `jobs`, not the reverse.

    The first version attached the list to the foreign key's own table, which
    reads as "episodes is referenced by episode_quality" when the constraint says
    the opposite - and the only reason this column exists is to tell an operator
    which side of a relationship owns the link.
    """
    with _scripted(
        [
            [_column("id", 1, table="builds")],
            [
                {
                    "source": "jobs",
                    "source_column": "job_id",
                    "target": "builds",
                    "target_column": "id",
                }
            ],
            {"n": 1},
        ]
    ):
        model = describe()
    assert model["tables"][0]["name"] == "builds"
    assert model["tables"][0]["referenced_by"] == ["jobs"]


def test_a_nullability_flag_becomes_a_boolean() -> None:
    with _scripted(
        [
            [_column("maybe", 1, is_nullable="YES")],
            [],
            {"n": 0},
        ]
    ):
        model = describe()
    assert model["tables"][0]["columns"][0]["nullable"] is True


def test_a_table_name_is_quoted_in_the_count_query() -> None:
    """`order` and `user` are real table names; unquoted they are syntax."""
    connection = Scripted([[_column("id", 1)], [], {"n": 3}])
    with patch("data_engine.catalog.introspect.connect", return_value=_connection(connection)):
        describe()
    assert any('FROM "episodes"' in s for s in connection.statements)


def test_an_unreachable_catalog_returns_a_renderable_model_instead_of_raising() -> None:
    with patch(
        "data_engine.catalog.introspect.connect",
        side_effect=RuntimeError("connection refused"),
    ):
        model = describe()

    assert model["reachable"] is False
    assert "connection refused" in model["error"]
    assert model["tables"] == []
    assert model["table_count"] == 0


def test_the_error_names_the_exception_so_a_500_is_diagnosable() -> None:
    with patch("data_engine.catalog.introspect.connect", side_effect=TimeoutError("no route")):
        model = describe()
    assert model["error"].startswith("TimeoutError:")


def test_a_query_that_returns_nothing_is_an_empty_catalog_not_a_crash() -> None:
    with _scripted([[], []]):
        model = describe()
    assert model["reachable"] is True
    assert model["tables"] == []
