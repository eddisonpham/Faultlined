"""PostgreSQL repository for vertical-slice jobs, episodes, artifacts, and lineage."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from psycopg.types.json import Jsonb

from data_engine.catalog.database import connect
from data_engine.config import Settings
from data_engine.jobs.state import ALLOWED_TRANSITIONS, JobState


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different request."""


class InvalidTransition(ValueError):
    """A job state transition is not allowed by the lifecycle contract."""


def canonical_json(value: Any) -> bytes:
    """Stable UTF-8 JSON bytes; rejects non-finite numbers by default."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


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
    ) -> tuple[dict[str, Any], bool]:
        request = {"type": job_type, "payload": payload}
        request_hash = hashlib.sha256(canonical_json(request)).hexdigest()
        job_id = str(uuid.uuid4())
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
                       id, type, state, payload, idempotency_key, request_hash, correlation_id
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (idempotency_key) DO NOTHING RETURNING *""",
                (
                    job_id,
                    job_type,
                    JobState.QUEUED.value,
                    Jsonb(payload),
                    idempotency_key,
                    request_hash,
                    correlation_id,
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
                """SELECT * FROM jobs WHERE state = %s
                   ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (JobState.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE jobs SET state = %s, started_at = now()
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

    def register_episode(
        self,
        *,
        source_hash: str,
        artifact_hash: str,
        size_bytes: int,
        metadata: dict[str, Any],
        job_id: str,
    ) -> dict[str, Any]:
        episode_id = str(uuid.uuid4())
        with connect(self.settings) as connection:
            connection.execute(
                """INSERT INTO artifacts (hash, size_bytes)
                   VALUES (%s, %s) ON CONFLICT (hash) DO NOTHING""",
                (artifact_hash, size_bytes),
            )
            row = connection.execute(
                """INSERT INTO episodes (id, source_hash, artifact_hash, format, metadata)
                   VALUES (%s, %s, %s, 'synthetic-json', %s)
                   ON CONFLICT (source_hash) DO UPDATE SET source_hash = EXCLUDED.source_hash
                   RETURNING *""",
                (episode_id, source_hash, artifact_hash, Jsonb(metadata)),
            ).fetchone()
            if row is None:
                raise RuntimeError("episode upsert returned no row")
            connection.execute(
                """INSERT INTO lineage_edges (from_type, from_ref, to_type, to_ref, relation)
                   VALUES ('episode', %s, 'job', %s, 'produced_by') ON CONFLICT DO NOTHING""",
                (row["id"], job_id),
            )
            return dict(row)

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
