"""Shared test helpers.

PostgreSQL-backed tests run against a dedicated `<name>_test` database rather than
the development one. They share a job queue, and a worker from `just run` would
otherwise claim the jobs a test submitted, making the suite flaky whenever the app
happens to be running.
"""

from __future__ import annotations

import os

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

TEST_DB_SUFFIX = "_test"


def _ensure_database(info: dict[str, str], dbname: str) -> None:
    with psycopg.connect(
        make_conninfo(**{**info, "dbname": "postgres"}), autocommit=True
    ) as maintenance:
        exists = maintenance.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if exists is None:
            maintenance.execute(f'CREATE DATABASE "{dbname}"')


def postgres_test_dsn() -> str:
    """DSN for the test database, or empty when no database is configured.

    Not named `test_*`: test modules import it, and pytest would collect the
    helper itself as a test.
    """
    base = os.environ.get("DE_DATABASE_URL")
    if not base:
        return ""
    info = conninfo_to_dict(base)
    dbname = info.get("dbname") or "data_engine"
    test_dbname = f"{dbname}{TEST_DB_SUFFIX}"
    _ensure_database(info, test_dbname)
    return make_conninfo(**{**info, "dbname": test_dbname})
