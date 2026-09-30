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
    nonfinite integer NOT NULL DEFAULT 0,
    max_gap_seconds double precision,
    gap_ratio double precision NOT NULL DEFAULT 0,
    integrity text NOT NULL DEFAULT 'unknown',
    worst_verdict text NOT NULL DEFAULT 'unknown',
    worst_dim text,
    judged_dims integer NOT NULL DEFAULT 0,
    motion_trace jsonb NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS builds (
    -- The content address. Two builds of the same episodes under the same policy
    -- and commit are the same build, so this is the primary key rather than a
    -- surrogate id: it makes rebuild determinism a schema constraint (NFR-004)
    -- instead of a convention the code has to remember to keep.
    hash text PRIMARY KEY,
    name text NOT NULL,
    -- The manifest that was hashed. Stored verbatim so a build can be explained
    -- without re-deriving it, and so the hash can be recomputed and compared.
    manifest jsonb NOT NULL,
    episode_count integer NOT NULL CHECK (episode_count >= 0),
    -- The validation policy in force, by content address. A build citing a
    -- policy that no longer exists would be unciteable, so this is a reference.
    profile_hash text REFERENCES validation_profiles(hash),
    code_commit text NOT NULL DEFAULT '',
    job_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS builds_created_idx ON builds (created_at);

CREATE TABLE IF NOT EXISTS episode_slices (
    id text PRIMARY KEY,
    name text NOT NULL DEFAULT '',
    notes text NOT NULL DEFAULT '',
    filter_config jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name)
);

CREATE INDEX IF NOT EXISTS episode_slices_name_idx ON episode_slices (name);

CREATE TABLE IF NOT EXISTS slice_memberships (
    slice_id text NOT NULL REFERENCES episode_slices(id) ON DELETE CASCADE,
    episode_id text NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    included_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (slice_id, episode_id)
);
CREATE INDEX IF NOT EXISTS slice_memberships_slice_idx ON slice_memberships (slice_id);

CREATE TABLE IF NOT EXISTS job_contracts (
    job_id text PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    expected_episodes integer,
    expected_valid_fraction double precision,
    max_duration_seconds double precision,
    deadline_at timestamptz,
    outcome text NOT NULL DEFAULT 'pending',
    observed jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS job_contracts_outcome_idx ON job_contracts (outcome);

CREATE TABLE IF NOT EXISTS monitor_baselines (
    feature text NOT NULL,
    scope text NOT NULL DEFAULT '',
    samples jsonb NOT NULL DEFAULT '[]',
    center double precision NOT NULL DEFAULT 0,
    breached boolean NOT NULL DEFAULT false,
    held_since double precision,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feature, scope)
);

CREATE TABLE IF NOT EXISTS monitor_incidents (
    id text PRIMARY KEY,
    fingerprint text NOT NULL,
    label text NOT NULL,
    severity text NOT NULL,
    notify_class text NOT NULL DEFAULT 'queue',
    scope text NOT NULL DEFAULT '',
    summary text NOT NULL DEFAULT '',
    evidence jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'open',
    occurrence_count integer NOT NULL DEFAULT 1,
    feature_schema_version integer NOT NULL DEFAULT 1,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz,
    resolved_at timestamptz
);
-- One unresolved incident per fingerprint. This is the dedup guarantee, enforced by
-- the database rather than by the writer, so a crash between the check and the insert
-- cannot open a second row for the same fault.
CREATE UNIQUE INDEX IF NOT EXISTS monitor_incidents_unresolved_idx
    ON monitor_incidents (fingerprint) WHERE status <> 'resolved';
CREATE INDEX IF NOT EXISTS monitor_incidents_last_seen_idx
    ON monitor_incidents (last_seen DESC);
CREATE INDEX IF NOT EXISTS monitor_incidents_status_idx ON monitor_incidents (status);
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
-- Temporal and diagnostic half of the quality record (ADR 0018 addendum). A
-- nullable max_gap_seconds is the honest "no clock was supplied", which is a
-- different fact from "the recording had no gaps".
ALTER TABLE episode_quality ADD COLUMN IF NOT EXISTS nonfinite integer NOT NULL DEFAULT 0;
ALTER TABLE episode_quality ADD COLUMN IF NOT EXISTS max_gap_seconds double precision;
ALTER TABLE episode_quality ADD COLUMN IF NOT EXISTS gap_ratio double precision NOT NULL DEFAULT 0;
ALTER TABLE episode_quality ADD COLUMN IF NOT EXISTS integrity text NOT NULL DEFAULT 'unknown';
ALTER TABLE episode_quality ADD COLUMN IF NOT EXISTS worst_verdict text NOT NULL DEFAULT 'unknown';
ALTER TABLE episode_quality ADD COLUMN IF NOT EXISTS worst_dim text;
ALTER TABLE episode_quality ADD COLUMN IF NOT EXISTS judged_dims integer NOT NULL DEFAULT 0;
ALTER TABLE episode_quality ADD COLUMN IF NOT EXISTS motion_trace jsonb NOT NULL DEFAULT '[]';

ALTER TABLE episode_slices ADD COLUMN IF NOT EXISTS name text NOT NULL DEFAULT '';
ALTER TABLE episode_slices ADD COLUMN IF NOT EXISTS notes text NOT NULL DEFAULT '';
ALTER TABLE episode_slices ADD COLUMN IF NOT EXISTS filter_config jsonb NOT NULL DEFAULT '{}';
ALTER TABLE episode_slices ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE slice_memberships
  ADD COLUMN IF NOT EXISTS included_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE job_contracts ADD COLUMN IF NOT EXISTS outcome text NOT NULL DEFAULT 'pending';
ALTER TABLE job_contracts ADD COLUMN IF NOT EXISTS observed jsonb NOT NULL DEFAULT '{}';
ALTER TABLE monitor_incidents
  ADD COLUMN IF NOT EXISTS notify_class text NOT NULL DEFAULT 'queue';
ALTER TABLE monitor_incidents
  ADD COLUMN IF NOT EXISTS feature_schema_version integer NOT NULL DEFAULT 1;
ALTER TABLE monitor_incidents
  ADD COLUMN IF NOT EXISTS acknowledged_at timestamptz;
ALTER TABLE monitor_incidents
  ADD COLUMN IF NOT EXISTS resolved_at timestamptz;

-- Build membership is a join table rather than an array in the manifest: the
-- manifest is hashed (and therefore frozen), while "which builds contain this
-- episode" is a query that has to stay fast as build count grows (FR-008).
CREATE TABLE IF NOT EXISTS build_episodes (
    build_hash text NOT NULL REFERENCES builds(hash) ON DELETE CASCADE,
    episode_id text NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    source_hash text NOT NULL,
    artifact_hash text NOT NULL,
    ordinal integer NOT NULL,
    PRIMARY KEY (build_hash, episode_id)
);

CREATE INDEX IF NOT EXISTS build_episodes_episode_idx ON build_episodes (episode_id);
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
