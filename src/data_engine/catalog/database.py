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
    source_hash text NOT NULL,
    episode_key text NOT NULL DEFAULT '',
    artifact_hash text NOT NULL REFERENCES artifacts(hash),
    format text NOT NULL,
    metadata jsonb NOT NULL,
    state text NOT NULL DEFAULT 'ingested',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_hash, episode_key)
);

CREATE TABLE IF NOT EXISTS validation_profiles (
    hash text PRIMARY KEY,
    name text NOT NULL,
    version text NOT NULL,
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS validation_results (
    id bigserial PRIMARY KEY,
    episode_id text NOT NULL REFERENCES episodes(id),
    profile_hash text NOT NULL,
    profile_name text NOT NULL DEFAULT '',
    profile_version text NOT NULL DEFAULT '',
    passed boolean NOT NULL,
    reason_codes jsonb NOT NULL,
    violations jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (episode_id, profile_hash)
);

CREATE TABLE IF NOT EXISTS episode_quality (
    episode_id text PRIMARY KEY REFERENCES episodes(id),
    frame_count integer NOT NULL,
    movement_score double precision NOT NULL,
    jerk_score double precision NOT NULL,
    stall_ratio double precision NOT NULL,
    verdict text NOT NULL,
    dims jsonb NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now()
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

# Columns the job lifecycle (retry / timeout / cancel) needs, added to pre-existing
# databases. CREATE TABLE IF NOT EXISTS does not add columns to a table that already
# exists, so these are separate idempotent statements.
_MIGRATIONS = """
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 3;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS deadline_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS worker_id text;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS episode_key text NOT NULL DEFAULT '';
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'ingested';
ALTER TABLE episodes DROP CONSTRAINT IF EXISTS episodes_source_hash_key;
CREATE UNIQUE INDEX IF NOT EXISTS episodes_source_key_idx ON episodes (source_hash, episode_key);
CREATE INDEX IF NOT EXISTS episodes_state_idx ON episodes (state);
ALTER TABLE validation_results ADD COLUMN IF NOT EXISTS profile_name text NOT NULL DEFAULT '';
ALTER TABLE validation_results ADD COLUMN IF NOT EXISTS profile_version text NOT NULL DEFAULT '';
"""


@contextmanager
def connect(settings: Settings | None = None) -> Iterator[Connection[dict[str, object]]]:
    """Open a short-lived connection; never log the DSN or credentials."""
    configured = settings or load_settings()
    # Bound the connect phase: libpq's default retries a refused or unroutable host for
    # minutes, which turns a stale DSN into an apparent hang rather than a fast error.
    with psycopg.connect(
        configured.database_url.get_secret_value(),
        row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    ) as connection:
        yield connection


CONNECT_TIMEOUT_SECONDS = 5


def initialize_schema(settings: Settings | None = None) -> None:
    """Create vertical-slice tables if absent; safe to call on every startup."""
    with connect(settings) as connection:
        for script in (_SCHEMA, _MIGRATIONS):
            for statement in script.split(";"):
                if statement.strip():
                    connection.execute(statement)
