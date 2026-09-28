"""PostgreSQL connection and initial schema for the vertical slice."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from data_engine.config import Settings, load_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id text PRIMARY KEY,
    type text NOT NULL,
    state text NOT NULL,
    payload jsonb NOT NULL,
    result jsonb,
    error jsonb,
    idempotency_key text UNIQUE,
    request_hash text NOT NULL,
    correlation_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_state_created_idx ON jobs (state, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
    hash text PRIMARY KEY,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS episodes (
    id text PRIMARY KEY,
    source_hash text NOT NULL UNIQUE,
    artifact_hash text NOT NULL REFERENCES artifacts(hash),
    format text NOT NULL,
    metadata jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lineage_edges (
    from_type text NOT NULL,
    from_ref text NOT NULL,
    to_type text NOT NULL,
    to_ref text NOT NULL,
    relation text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (from_type, from_ref, to_type, to_ref, relation)
);
"""


@contextmanager
def connect(settings: Settings | None = None) -> Iterator[Connection[dict[str, object]]]:
    """Open a short-lived connection; never log the DSN or credentials."""
    configured = settings or load_settings()
    with psycopg.connect(
        configured.database_url.get_secret_value(), row_factory=dict_row
    ) as connection:
        yield connection


def initialize_schema(settings: Settings | None = None) -> None:
    """Create vertical-slice tables if absent; safe to call on every startup."""
    with connect(settings) as connection:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                connection.execute(statement)
