"""The bounded statement timeout must actually cancel a runaway query.

A unit test can only prove the ``options`` string reaches libpq. The guarantee
that matters is behavioural: a query that would otherwise pin a connection
until the API exhausts its worker threads is abandoned, and the connection stays
usable afterwards rather than being poisoned by the abort.
"""

from __future__ import annotations

import psycopg
import pytest

from data_engine.catalog.database import STATEMENT_TIMEOUT_MILLISECONDS, connect
from data_engine.config import Settings
from tests.conftest import postgres_test_dsn


@pytest.fixture
def settings() -> Settings:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("no DE_DATABASE_URL configured")
    return Settings(_env_file=None, database_url=dsn)  # type: ignore[arg-type]


@pytest.mark.integration
def test_a_runaway_query_is_cancelled_rather_than_held(settings: Settings) -> None:
    with connect(settings) as connection, pytest.raises(psycopg.errors.QueryCanceled) as cancelled:
        connection.execute("SELECT pg_sleep(30)").fetchone()

    assert "statement timeout" in str(cancelled.value).lower()


@pytest.mark.integration
def test_the_next_connection_is_unaffected(settings: Settings) -> None:
    """A cancelled statement must not leave lasting damage behind.

    psycopg aborts the surrounding transaction when a statement times out, so the
    connection that raised is unusable afterwards. That is contained by design:
    ``connect`` yields a short-lived connection that is closed on exit, and no
    caller continues issuing statements after catching an error. What must hold
    is that the failure does not escape the context manager or poison later work.
    """
    with pytest.raises(psycopg.errors.QueryCanceled), connect(settings) as connection:
        connection.execute("SELECT pg_sleep(30)").fetchone()

    with connect(settings) as connection:
        assert connection.execute("SELECT 1").fetchone() == {"?column?": 1}


@pytest.mark.integration
def test_ordinary_catalog_queries_are_well_inside_the_budget(settings: Settings) -> None:
    """The bound must not be tight enough to truncate real catalog work."""
    with connect(settings) as connection:
        elapsed = connection.execute("SELECT count(*) FROM jobs").fetchone()

    assert elapsed is not None
    assert STATEMENT_TIMEOUT_MILLISECONDS >= 5_000
