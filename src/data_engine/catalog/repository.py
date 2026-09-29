"""PostgreSQL repository for vertical-slice jobs, episodes, artifacts, and lineage."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from psycopg.types.json import Jsonb

from data_engine.canonical import canonical_json as _canonical_json
from data_engine.catalog.database import connect
from data_engine.config import Settings
from data_engine.jobs.state import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    DEFAULT_MAX_ATTEMPTS,
    JobState,
)
from data_engine.validation.profile import ValidationProfile, profile_hash


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different request."""


class InvalidTransition(ValueError):
    """A job state transition is not allowed by the lifecycle contract."""


def canonical_json(value: Any) -> bytes:
    """Re-exported from [data_engine.canonical](../canonical.py); kept here because the
    idempotency request hash has always been imported from this module."""
    return _canonical_json(value)


class PostgresCatalog:
    """Persistence operations used by the phase-05 ingest-job slice."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings

    def submit_job(
        self,
        job_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        correlation_id: str,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        deadline_seconds: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Queue a job.

        ``max_attempts`` bounds retries (F6); ``deadline_seconds`` sets a wall-clock
        budget measured from submission (F6). Both are part of the idempotency
        request, so replaying with different retry policy is a conflict.
        """
        request = {
            "type": job_type,
            "payload": payload,
            "max_attempts": max_attempts,
            "deadline_seconds": deadline_seconds,
        }
        request_hash = hashlib.sha256(canonical_json(request)).hexdigest()
        job_id = str(uuid.uuid4())
        deadline_at = (
            datetime.now(UTC) + timedelta(seconds=deadline_seconds)
            if deadline_seconds is not None
            else None
        )
        with connect(self.settings) as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = %s", (idempotency_key,)
                ).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise IdempotencyConflict(idempotency_key)
                    return dict(existing), False
            row = connection.execute(
                """INSERT INTO jobs (
                       id, type, state, payload, idempotency_key, request_hash, correlation_id,
                       max_attempts, deadline_at
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (idempotency_key) DO NOTHING RETURNING *""",
                (
                    job_id,
                    job_type,
                    JobState.QUEUED.value,
                    Jsonb(payload),
                    idempotency_key,
                    request_hash,
                    correlation_id,
                    max_attempts,
                    deadline_at,
                ),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = %s", (idempotency_key,)
                ).fetchone()
                if existing and existing["request_hash"] == request_hash:
                    return dict(existing), False
                raise IdempotencyConflict(idempotency_key)
        return dict(row), True

    def claim_job(self) -> dict[str, Any] | None:
        with connect(self.settings) as connection:
            row = connection.execute(
                """SELECT * FROM jobs
                   WHERE state = %s AND attempts < max_attempts
                   ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (JobState.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE jobs SET state = %s, started_at = now(), attempts = attempts + 1
                   WHERE id = %s RETURNING *""",
                (JobState.RUNNING.value, row["id"]),
            ).fetchone()
            if updated is None:
                raise RuntimeError("claimed job disappeared during update")
            return dict(updated)

    def finish_job(
        self,
        job_id: str,
        state: JobState,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = JobState(cast(str, row["state"]))
            if state not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"{current.value} -> {state.value}")
            updated = connection.execute(
                """UPDATE jobs SET state = %s, result = %s, error = %s, finished_at = %s
                   WHERE id = %s RETURNING *""",
                (
                    state.value,
                    Jsonb(result) if result is not None else None,
                    Jsonb(error) if error is not None else None,
                    datetime.now(UTC),
                    job_id,
                ),
            ).fetchone()
            if updated is None:
                raise RuntimeError("job disappeared during transition")
        return dict(updated)

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        """Ask a running or queued job to stop (F7: cooperative cancellation).

        Queued jobs cancel outright; running jobs move to `cancel_requested` so the
        worker can finish its current unit of work and observe the request.
        """
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = JobState(cast(str, row["state"]))
            target = JobState.CANCELED if current is JobState.QUEUED else JobState.CANCEL_REQUESTED
            if target not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"{current.value} -> {target.value}")
            updated = connection.execute(
                """UPDATE jobs SET state = %s, finished_at = now() WHERE id = %s RETURNING *""",
                (target.value, job_id),
            ).fetchone()
        if updated is None:
            raise RuntimeError("job disappeared during cancel")
        return dict(updated)

    def is_cancel_requested(self, job_id: str) -> bool:
        """Whether a cancel was requested while this job was running."""
        with connect(self.settings) as connection:
            row = connection.execute("SELECT state FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            return False
        return cast(str, row["state"]) == JobState.CANCEL_REQUESTED.value

    def requeue_for_retry(self, job_id: str) -> dict[str, Any]:
        """Return a failed attempt to the queue (retrying -> queued).

        Only valid while attempts remain; otherwise the job must be marked failed.
        """
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = JobState(cast(str, row["state"]))
            if current is not JobState.RETRYING:
                raise InvalidTransition(f"{current.value} -> retrying")
            if cast(int, row["attempts"]) >= cast(int, row["max_attempts"]):
                raise ValueError("attempts exhausted; job must be marked failed")
            updated = connection.execute(
                """UPDATE jobs SET state = %s, error = NULL, result = NULL
                   WHERE id = %s RETURNING *""",
                (JobState.QUEUED.value, job_id),
            ).fetchone()
        if updated is None:
            raise RuntimeError("job disappeared during retry")
        return dict(updated)

    def mark_deadline_expired(self, job_id: str) -> dict[str, Any]:
        """Move a job whose deadline passed to `timed_out` (F6)."""
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = JobState(cast(str, row["state"]))
            if JobState.TIMED_OUT not in ALLOWED_TRANSITIONS[current]:
                raise InvalidTransition(f"{current.value} -> timed_out")
            updated = connection.execute(
                """UPDATE jobs SET state = %s, finished_at = now(),
                          error = jsonb_build_object('type', 'DeadlineExceeded',
                                                     'message', 'job exceeded its deadline')
                   WHERE id = %s RETURNING *""",
                (JobState.TIMED_OUT.value, job_id),
            ).fetchone()
        if updated is None:
            raise RuntimeError("job disappeared during timeout")
        return dict(updated)

    def reap_expired_deadlines(self) -> int:
        """Time out every non-terminal job whose deadline has passed.

        Run periodically; returns how many jobs were reaped.
        """
        with connect(self.settings) as connection:
            rows = connection.execute(
                """UPDATE jobs SET state = %s, finished_at = now(),
                          error = jsonb_build_object('type', 'DeadlineExceeded',
                                                     'message', 'job exceeded its deadline')
                   WHERE deadline_at IS NOT NULL
                     AND deadline_at < now()
                     AND state = ANY(%s)
                   RETURNING id""",
                (JobState.TIMED_OUT.value, [s.value for s in ACTIVE_STATES]),
            ).fetchall()
        return len(rows)

    def register_episode(
        self,
        *,
        source_hash: str,
        artifact_hash: str,
        size_bytes: int,
        metadata: dict[str, Any],
        job_id: str,
        episode_key: str | None = None,
        episode_format: str = "synthetic-json",
    ) -> dict[str, Any]:
        """Register one episode, idempotently on (source_hash, episode_key).

        A content-addressed source file can hold many episodes: a LeRobot v3 Parquet
        shard carries every episode in its chunk. So the identity is the pair, not the
        file hash alone, and re-ingesting the same episode is a no-op rather than a
        second row.
        """
        episode_id = str(uuid.uuid4())
        key = episode_key or ""
        with connect(self.settings) as connection:
            connection.execute(
                """INSERT INTO artifacts (hash, size_bytes)
                   VALUES (%s, %s) ON CONFLICT (hash) DO NOTHING""",
                (artifact_hash, size_bytes),
            )
            row = connection.execute(
                """INSERT INTO episodes
                       (id, source_hash, episode_key, artifact_hash, format, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (source_hash, episode_key) DO UPDATE
                       SET episode_key = EXCLUDED.episode_key
                   RETURNING *""",
                (episode_id, source_hash, key, artifact_hash, episode_format, Jsonb(metadata)),
            ).fetchone()
            if row is None:
                raise RuntimeError("episode upsert returned no row")
            connection.execute(
                """INSERT INTO lineage_edges (from_type, from_ref, to_type, to_ref, relation)
                   VALUES ('episode', %s, 'job', %s, 'produced_by') ON CONFLICT DO NOTHING""",
                (row["id"], job_id),
            )
            return dict(row)

    def register_validation_profile(self, profile: ValidationProfile) -> dict[str, Any]:
        """Persist a profile under its content address (ADR 0016).

        Idempotent on both the hash and (name, version): two submissions of the same
        document are the same profile, and a name/version collision with different content
        is a conflict rather than a silent overwrite, because a build manifest citing a
        hash must never resolve to two different policies.
        """
        document = profile.to_dict()
        payload_hash = profile_hash(profile)
        with connect(self.settings) as connection:
            existing = connection.execute(
                "SELECT hash FROM validation_profiles WHERE name = %s AND version = %s",
                (profile.name, profile.version),
            ).fetchone()
            if existing is not None:
                if existing["hash"] != payload_hash:
                    raise IdempotencyConflict(f"{profile.name}@{profile.version}")
                return {"hash": payload_hash, "created": False}
            row = connection.execute(
                """INSERT INTO validation_profiles (hash, name, version, document)
                   VALUES (%s, %s, %s, %s) RETURNING hash""",
                (payload_hash, profile.name, profile.version, Jsonb(document)),
            ).fetchone()
        if row is None:
            raise RuntimeError("validation profile insert returned no row")
        return {"hash": payload_hash, "created": True}

    def record_validation(
        self,
        *,
        episode_id: str,
        profile_hash: str,
        profile_name: str,
        profile_version: str,
        passed: bool,
        reason_codes: list[str],
        violations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Record an immutable result and move the episode to `valid` or `quarantined`.

        Re-validating the same episode under the same profile replaces the row, because
        re-running an identical check must not accumulate duplicate "history" of a
        statement that has not changed. A *different* profile is a new row.
        """
        state = "valid" if passed else "quarantined"
        with connect(self.settings) as connection:
            row = connection.execute(
                """INSERT INTO validation_results
                       (episode_id, profile_hash, profile_name, profile_version,
                        passed, reason_codes, violations)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (episode_id, profile_hash) DO UPDATE
                       SET passed = EXCLUDED.passed,
                           reason_codes = EXCLUDED.reason_codes,
                           violations = EXCLUDED.violations,
                           created_at = now()
                   RETURNING id, passed""",
                (
                    episode_id,
                    profile_hash,
                    profile_name,
                    profile_version,
                    passed,
                    Jsonb(reason_codes),
                    Jsonb(violations),
                ),
            ).fetchone()
            connection.execute("UPDATE episodes SET state = %s WHERE id = %s", (state, episode_id))
        if row is None:
            raise RuntimeError("validation result insert returned no row")
        return {"id": row["id"], "passed": row["passed"], "state": state}

    def get_validation_results(self, episode_id: str) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            rows = connection.execute(
                """SELECT id, profile_hash, profile_name, profile_version,
                          passed, reason_codes, violations, created_at
                   FROM validation_results WHERE episode_id = %s ORDER BY id""",
                (episode_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_jobs(self, state: JobState) -> int:
        """Number of jobs currently in ``state``; feeds the queue-depth gauge."""
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT count(*) AS total FROM jobs WHERE state = %s", (state.value,)
            ).fetchone()
        if row is None:
            return 0
        return cast(int, row["total"])

    def list_jobs(
        self,
        *,
        state: JobState | None = None,
        job_type: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first job page, cursor-based on ``created_at``.

        The UI never asks for every row, so this is bounded and returns an optional
        ``next_before`` cursor for the following page.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state = %s")
            params.append(state.value)
        if job_type is not None:
            clauses.append("type = %s")
            params.append(job_type)
        if before is not None:
            clauses.append("created_at < %s")
            params.append(before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with connect(self.settings) as connection:
            rows = connection.execute(
                f"""SELECT id, type, state, correlation_id, error, attempts, max_attempts,
                          created_at, started_at, finished_at
                    FROM jobs {where}
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_artifacts(
        self, *, limit: int = 50, before: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Newest-first artifact page with the episodes that reference each artifact."""
        params: list[Any] = []
        clause = ""
        if before is not None:
            clause = "WHERE a.created_at < %s"
            params.append(before)
        params.append(limit)
        with connect(self.settings) as connection:
            rows = connection.execute(
                f"""SELECT a.hash, a.size_bytes, a.created_at,
                           coalesce(
                               (SELECT jsonb_agg(e.id) FROM episodes e
                                WHERE e.artifact_hash = a.hash),
                               '[]'::jsonb
                           ) AS episode_ids
                    FROM artifacts a
                    {clause}
                    ORDER BY a.created_at DESC, a.hash DESC
                    LIMIT %s""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def count_artifacts(self) -> int:
        """Total artifacts stored; feeds the Status page."""
        with connect(self.settings) as connection:
            row = connection.execute("SELECT count(*) AS total FROM artifacts").fetchone()
        return cast(int, row["total"]) if row else 0

    def count_episodes(self) -> int:
        """Total registered episodes; feeds the Status page."""
        with connect(self.settings) as connection:
            row = connection.execute("SELECT count(*) AS total FROM episodes").fetchone()
        return cast(int, row["total"]) if row else 0

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with connect(self.settings) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT * FROM episodes WHERE id = %s", (episode_id,)
            ).fetchone()
            if row is None:
                return None
            edges = connection.execute(
                """SELECT from_type, from_ref, to_type, to_ref, relation
                   FROM lineage_edges WHERE from_type = 'episode' AND from_ref = %s""",
                (episode_id,),
            ).fetchall()
        result = dict(row)
        result["lineage"] = [dict(edge) for edge in edges]
        return result

    def close(self) -> None:
        """Compatibility no-op; connections are short lived."""
