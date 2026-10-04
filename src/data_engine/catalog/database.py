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

-- Cluster proposals (ADR 0026). Two tables and one join, because the thing being
-- stored is a *proposal*: a suggested grouping of task strings that a human confirms.
--
-- `task_clusters.key` is a hash of the proposal's primary core, not a surrogate row id,
-- so a rebuild can match a stored confirmation to the proposal it was about even though
-- the row was recreated. That is the whole reason a confirm survives a recompute.
--
-- Frozen state lives in `cluster_confirmations` rather than as columns here: a
-- proposal with no confirmation row is unconfirmed, and there is exactly one way for
-- that to be true, so it cannot drift out of sync with a boolean.
--
-- A confirmation deliberately does **not** reference `task_clusters`. Its subject is the
-- set of task strings a human grouped, not the row that currently holds them: a rebuild
-- re-keys every proposal whenever the extraction options change, and a foreign key took
-- the confirmations with it (measured, not assumed - the first version did exactly that,
-- and `ON DELETE CASCADE` plus a re-keyed core lost every label in one rebuild).
-- `tasks` carries the set, and a rebuild re-attaches each confirmation to whichever
-- proposal now contains those strings.
CREATE TABLE IF NOT EXISTS task_clusters (
    key text PRIMARY KEY,
    core text NOT NULL,
    episodes integer NOT NULL DEFAULT 0,
    task_count integer NOT NULL DEFAULT 0,
    merged_cores jsonb NOT NULL DEFAULT '[]',
    centroid jsonb,
    source text NOT NULL DEFAULT 'catalog',
    ignored text NOT NULL DEFAULT '',
    radius double precision NOT NULL DEFAULT 0.3,
    rule text NOT NULL DEFAULT 'running_mean',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS task_cluster_members (
    cluster_key text NOT NULL REFERENCES task_clusters(key) ON DELETE CASCADE,
    task text NOT NULL,
    core text NOT NULL,
    episodes integer NOT NULL DEFAULT 0,
    verb text NOT NULL DEFAULT '',
    colours jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (cluster_key, task)
);

CREATE INDEX IF NOT EXISTS task_cluster_members_task_idx ON task_cluster_members (task);

-- Human disposition of uncertain strings is independent from the automatic proposal
-- partition. Review must not rewrite episode metadata or pretend to retrain the engine.
CREATE TABLE IF NOT EXISTS cluster_review_decisions (
    task text PRIMARY KEY,
    disposition text NOT NULL CHECK (disposition IN ('class', 'dismissed')),
    label text NOT NULL DEFAULT '',
    reviewed_at timestamptz NOT NULL DEFAULT now(),
    reviewed_by text NOT NULL DEFAULT 'operator'
);

CREATE INDEX IF NOT EXISTS cluster_review_decisions_disposition_idx
    ON cluster_review_decisions (disposition, reviewed_at DESC);

CREATE TABLE IF NOT EXISTS cluster_confirmations (
    id bigserial PRIMARY KEY,
    label text NOT NULL,
    tasks jsonb NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    confirmed_by text NOT NULL DEFAULT 'operator'
);

-- One run per rebuild, so the page can say when these numbers were computed instead of
-- implying they are current.
CREATE TABLE IF NOT EXISTS cluster_runs (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    ignored text NOT NULL DEFAULT '',
    radius double precision NOT NULL,
    rule text NOT NULL,
    health jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cluster_runs_created_idx ON cluster_runs (created_at DESC);

-- Task vocabulary (ADR 0029). The curated vocabulary is the source of truth for what a
-- task string means - cluster proposals are one input to it, not the product.
-- (No semicolons in these comments: the DDL splitter breaks statements on them.)
--
-- An entry's id is derived from its preferred label at creation (voc_<blake2b(label)>),
-- which makes the migration-0002 backfill deterministic and collapses duplicate labels.
-- The id names the *entry*, so a rename keeps it.
--
-- A mapping is where a task string lives: an entry, or deliberately nowhere
-- (provenance 'dismiss', entry NULL - the reviewed noise that must not re-queue forever).
-- Episode rows keep their raw task string. This table is a view over them, so a vocabulary
-- edit never rewrites the audit trail.
CREATE TABLE IF NOT EXISTS task_vocabulary_entries (
    id text PRIMARY KEY,
    preferred_label text NOT NULL UNIQUE,
    core text NOT NULL DEFAULT '',
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT 'operator'
);

CREATE TABLE IF NOT EXISTS task_vocabulary_mappings (
    task_string text PRIMARY KEY,
    entry_id text REFERENCES task_vocabulary_entries(id) ON DELETE CASCADE,
    provenance text NOT NULL
        CHECK (provenance IN ('ingest', 'confirm', 'merge', 'split', 'dismiss')),
    mapped_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT task_vocabulary_mappings_target
        CHECK ((provenance = 'dismiss') = (entry_id IS NULL))
);

CREATE INDEX IF NOT EXISTS task_vocabulary_mappings_entry_idx
    ON task_vocabulary_mappings (entry_id);

-- Merge, split and rename events. The payload carries what the operation moved and what
-- it replaced, so undo is a compensating action over recorded facts rather than a guess.
CREATE TABLE IF NOT EXISTS task_vocabulary_events (
    id bigserial PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('merge', 'split', 'label', 'map', 'dismiss')),
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT 'operator',
    undone_at timestamptz
);

-- The migration ledger (ADR 0028). initialize_schema writes the baseline row
-- here, and `de migrate` appends one row per applied migration, so the schema
-- state a database is in is recorded in the database itself.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    name text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
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
-- The first draft keyed a confirmation on the proposal hash with a cascading reference,
-- which deleted every label on any rebuild that re-keyed the proposals. The table is
-- altered in place rather than dropped: an existing database has confirmations in it, and
-- CREATE TABLE IF NOT EXISTS will not add the columns this shape needs.
ALTER TABLE cluster_confirmations DROP CONSTRAINT IF EXISTS cluster_confirmations_pkey;
ALTER TABLE cluster_confirmations DROP COLUMN IF EXISTS cluster_key;
ALTER TABLE cluster_confirmations ADD COLUMN IF NOT EXISTS id bigserial;
ALTER TABLE cluster_confirmations ADD COLUMN IF NOT EXISTS tasks jsonb NOT NULL DEFAULT '[]';
ALTER TABLE cluster_confirmations ADD PRIMARY KEY (id);
CREATE INDEX IF NOT EXISTS cluster_confirmations_tasks_idx
    ON cluster_confirmations (md5(tasks::text));
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
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MILLISECONDS}",
    ) as connection:
        yield connection


CONNECT_TIMEOUT_SECONDS = 5
# A runaway query must fail instead of pinning a pooled connection until the API
# exhausts its worker threads. Every statement here is a catalog read or a
# small executemany, so anything past this is a lock or plan problem worth surfacing.
STATEMENT_TIMEOUT_MILLISECONDS = 15_000


#: The version `initialize_schema` records for itself (ADR 0028): the idempotent
#: DDL *is* the baseline migration, so a database the code has merely started
#: already reads as baseline-applied and `de migrate` runs only real pending work.
BASELINE_VERSION = "0001"
BASELINE_NAME = "baseline"


def run_schema_ddl(connection: Connection[dict[str, object]]) -> None:
    """Execute the idempotent DDL on an open connection.

    Split out of `initialize_schema` so the baseline migration (ADR 0028) can
    run the same statements inside its own transaction instead of opening a
    second connection mid-migration.
    """
    for script in (_SCHEMA, _MIGRATIONS):
        for statement in script.split(";"):
            if statement.strip():
                connection.execute(statement)


def initialize_schema(settings: Settings | None = None) -> None:
    """Create vertical-slice tables if absent; safe to call on every startup."""
    with connect(settings) as connection:
        run_schema_ddl(connection)
        # Record the baseline (ADR 0028). The table exists because _SCHEMA now
        # creates it; ON CONFLICT keeps every later start a silent no-op.
        connection.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (%s, %s) "
            "ON CONFLICT (version) DO NOTHING",
            (BASELINE_VERSION, BASELINE_NAME),
        )
        connection.commit()
