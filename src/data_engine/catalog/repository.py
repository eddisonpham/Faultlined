"""PostgreSQL repository for vertical-slice jobs, episodes, artifacts, and lineage."""

from __future__ import annotations

import functools
import hashlib
import time
import uuid
from collections.abc import Callable, Sequence
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
from data_engine.observability.metrics import RuntimeMetrics
from data_engine.validation.profile import ValidationProfile, profile_hash


def _timed_operation[Method: Callable[..., Any]](method: Method) -> Method:
    """Emit ``catalog_query_duration_seconds`` for one repository operation."""

    @functools.wraps(method)
    def wrapper(self: PostgresCatalog, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return method(self, *args, **kwargs)
        finally:
            self._metrics.catalog_query(time.perf_counter() - started, operation=method.__name__)

    return cast(Method, wrapper)


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different request."""


class InvalidTransition(ValueError):
    """A job state transition is not allowed by the lifecycle contract."""


def canonical_json(value: Any) -> bytes:
    """Re-exported from [data_engine.canonical](../canonical.py); kept here because the
    idempotency request hash has always been imported from this module."""
    return _canonical_json(value)


def _zscore(value: float, stats: dict[str, Any] | None) -> float:
    mean = (stats or {}).get("mean")
    std = (stats or {}).get("std")
    if mean is None or not std:
        return 0.0
    return (float(value) - float(mean)) / float(std)


def _assemble_quality_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-episode quality rows into distributions and outlier lists."""
    lengths = [int(row["frame_count"]) for row in rows]
    zscores = {
        str(row["episode_id"]): _zscore(
            row["frame_count"], {"mean": _mean(lengths), "std": _std(lengths)}
        )
        for row in rows
    }

    dim_names = sorted({dim["name"] for row in rows for dim in row.get("dims") or []})
    heat_episodes = []
    for row in rows:
        by_name = {dim["name"]: dim for dim in row.get("dims") or []}
        heat_episodes.append(
            {
                "episode_id": row["episode_id"],
                "values": [
                    by_name[name]["norm_delta_std"] if name in by_name else None
                    for name in dim_names
                ],
            }
        )

    def top(key: str, *, absolute: bool = False) -> list[dict[str, Any]]:
        ranked = sorted(
            rows,
            key=lambda row: abs(float(row[key])) if absolute else float(row[key]),
            reverse=True,
        )
        return [
            {
                "episode_id": row["episode_id"],
                "episode_key": row.get("episode_key"),
                "value": float(row[key]),
                "verdict": row["verdict"],
            }
            for row in ranked[:5]
        ]

    verdicts: dict[str, int] = {}
    for row in rows:
        verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1

    return {
        "episode_count": len(rows),
        "verdicts": verdicts,
        "length": _length_summary(lengths),
        "speed_distribution": [
            {
                "episode_id": row["episode_id"],
                "movement_score": float(row["movement_score"]),
                "verdict": row["verdict"],
            }
            for row in rows
        ],
        "heat_matrix": {"dims": dim_names, "episodes": heat_episodes},
        "outliers": {
            "jerk": top("jerk_score"),
            "stall": top("stall_ratio"),
            "length": sorted(
                [
                    {
                        "episode_id": row["episode_id"],
                        "episode_key": row.get("episode_key"),
                        "value": zscores[str(row["episode_id"])],
                        "verdict": row["verdict"],
                    }
                    for row in rows
                ],
                key=lambda item: abs(item["value"]),
                reverse=True,
            )[:5],
        },
    }


def _assemble_job_report(
    job: dict[str, Any], episodes: list[dict[str, Any]], validations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fold one run's produced episodes and verdicts into a triage report."""
    states: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    flags = {"jerky": 0, "stalled": 0}
    for row in episodes:
        states[row["state"]] = states.get(row["state"], 0) + 1
        verdict = row.get("verdict")
        if verdict:
            verdicts[str(verdict)] = verdicts.get(str(verdict), 0) + 1
            if verdict == "jerky":
                flags["jerky"] += 1
        if row.get("stall_ratio") is not None and float(row["stall_ratio"]) >= 0.5:
            flags["stalled"] += 1

    reason_codes: dict[str, int] = {}
    evaluated: set[str] = set()
    passed = 0
    for result in validations:
        evaluated.add(str(result["episode_id"]))
        if result["passed"]:
            passed += 1
        for code in result.get("reason_codes") or []:
            reason_codes[str(code)] = reason_codes.get(str(code), 0) + 1

    scored = [row for row in episodes if row.get("movement_score") is not None]
    return {
        "job": job,
        "episodes": {
            "total": len(episodes),
            "by_state": states,
            "verdicts": verdicts,
            "flags": flags,
            "length": _length_summary(
                [int(row["frame_count"]) for row in scored if row.get("frame_count") is not None]
            ),
            "mean_movement_score": _mean([float(row["movement_score"]) for row in scored]),
            "mean_jerk_score": _mean([float(row["jerk_score"]) for row in scored]),
            "mean_stall_ratio": _mean([float(row["stall_ratio"]) for row in scored]),
        },
        "validation": {
            "episodes_evaluated": len(evaluated),
            "results": len(validations),
            "passed": passed,
            "failed": len(validations) - passed,
            "reason_codes": reason_codes,
        },
    }


def _length_summary(lengths: list[int]) -> dict[str, Any]:
    if not lengths:
        return {"count": 0, "mean": 0.0, "std": 0.0, "min": 0, "max": 0, "histogram": []}
    lo, hi = min(lengths), max(lengths)
    bins = 10
    width = (hi - lo) / bins or 1
    histogram = [
        {"lo": lo + int(i * width), "hi": lo + int((i + 1) * width), "count": 0}
        for i in range(bins)
    ]
    for length in lengths:
        index = min(int((length - lo) / width), bins - 1)
        histogram[index]["count"] += 1
    return {
        "count": len(lengths),
        "mean": _mean(lengths),
        "std": _std(lengths),
        "min": lo,
        "max": hi,
        "histogram": histogram,
    }


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return float(variance**0.5)


class PostgresCatalog:
    """Persistence operations used by the phase-05 ingest-job slice."""

    def __init__(
        self, settings: Settings | None = None, *, metrics: RuntimeMetrics | None = None
    ) -> None:
        self.settings = settings
        # No sink means a no-op recorder: callers opt in to query telemetry.
        self._metrics = metrics or RuntimeMetrics()

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

    def episodes_produced_by(self, job_id: str) -> list[dict[str, Any]]:
        """Episodes registered by one job, following the reverse lineage edge."""
        with connect(self.settings) as connection:
            rows = connection.execute(
                """SELECT e.id, e.episode_key, e.format, e.state, e.created_at
                   FROM lineage_edges l
                   JOIN episodes e ON e.id = l.from_ref
                   WHERE l.from_type = 'episode' AND l.to_type = 'job'
                     AND l.to_ref = %s AND l.relation = 'produced_by'
                   ORDER BY e.created_at""",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def job_report(self, job_id: str) -> dict[str, Any] | None:
        """Triage report for one run: outputs, quality rollup, verdict counts."""
        with connect(self.settings) as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
            if job is None:
                return None
            episodes = connection.execute(
                """SELECT e.id, e.state, q.frame_count, q.movement_score,
                          q.jerk_score, q.stall_ratio, q.verdict
                   FROM lineage_edges l
                   JOIN episodes e ON e.id = l.from_ref
                   LEFT JOIN episode_quality q ON q.episode_id = e.id
                   WHERE l.from_type = 'episode' AND l.to_type = 'job'
                     AND l.to_ref = %s AND l.relation = 'produced_by'
                   ORDER BY e.created_at""",
                (job_id,),
            ).fetchall()
            validations = connection.execute(
                """SELECT v.episode_id, v.passed, v.reason_codes
                   FROM validation_results v
                   WHERE v.episode_id IN (
                       SELECT l.from_ref FROM lineage_edges l
                       WHERE l.from_type = 'episode' AND l.to_type = 'job'
                         AND l.to_ref = %s AND l.relation = 'produced_by')
                   ORDER BY v.id""",
                (job_id,),
            ).fetchall()
        return _assemble_job_report(
            dict(job), [dict(row) for row in episodes], [dict(row) for row in validations]
        )

    def record_episode_quality(self, episode_id: str, quality: dict[str, Any]) -> dict[str, Any]:
        """Persist the motion-quality summary computed at ingest (ADR 0018).

        A content-addressed episode recomputes identical signals on re-ingest, so
        the row is replaced rather than accumulated.
        """
        with connect(self.settings) as connection:
            row = connection.execute(
                """INSERT INTO episode_quality
                       (episode_id, frame_count, movement_score, jerk_score,
                        stall_ratio, verdict, dims)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (episode_id) DO UPDATE
                       SET frame_count = EXCLUDED.frame_count,
                           movement_score = EXCLUDED.movement_score,
                           jerk_score = EXCLUDED.jerk_score,
                           stall_ratio = EXCLUDED.stall_ratio,
                           verdict = EXCLUDED.verdict,
                           dims = EXCLUDED.dims,
                           computed_at = now()
                   RETURNING episode_id""",
                (
                    episode_id,
                    int(quality["frame_count"]),
                    float(quality["movement_score"]),
                    float(quality["jerk_score"]),
                    float(quality["stall_ratio"]),
                    str(quality["verdict"]),
                    Jsonb(quality.get("dims") or []),
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("episode quality insert returned no row")
        return {"episode_id": row["episode_id"], "verdict": str(quality["verdict"])}

    def get_episode_quality(self, episode_id: str) -> dict[str, Any] | None:
        """Quality signals for one episode; length z-score is computed at read time.

        Episode lengths do not change, but the population they are compared against
        grows, so a stored z-score would quietly go stale.
        """
        with connect(self.settings) as connection:
            row = connection.execute(
                """SELECT episode_id, frame_count, movement_score, jerk_score,
                          stall_ratio, verdict, dims, computed_at
                   FROM episode_quality WHERE episode_id = %s""",
                (episode_id,),
            ).fetchone()
            stats = connection.execute(
                """SELECT avg(frame_count) AS mean, stddev_samp(frame_count) AS std
                   FROM episode_quality"""
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["length_zscore"] = _zscore(cast(float, row["frame_count"]), stats)
        return result

    def quality_summary(self) -> dict[str, Any]:
        """Dataset-level quality view for the insights page and curation triage."""
        with connect(self.settings) as connection:
            rows = connection.execute(
                """SELECT q.episode_id, e.episode_key, q.frame_count, q.movement_score,
                          q.jerk_score, q.stall_ratio, q.verdict, q.dims
                   FROM episode_quality q JOIN episodes e ON e.id = q.episode_id
                   ORDER BY q.movement_score DESC"""
            ).fetchall()
        return _assemble_quality_summary([dict(row) for row in rows])

    def list_episodes(
        self, *, limit: int = 50, state: str | None = None, flag: str | None = None
    ) -> list[dict[str, Any]]:
        """Episode page with quality columns for the Episodes UI.

        Flags are curation views: `jerky`/`stalled` filter on quality signals and
        rank by the signal, `short`/`long` reorder by frame count so the tails of
        the length distribution surface first. Default is newest first.
        """
        where = []
        params: list[Any] = []
        order = "e.created_at DESC"
        if state:
            where.append("e.state = %s")
            params.append(state)
        if flag == "jerky":
            where.append("q.verdict = 'jerky'")
            order = "q.jerk_score DESC"
        elif flag == "stalled":
            where.append("q.stall_ratio >= 0.5")
            order = "q.stall_ratio DESC"
        elif flag == "short":
            order = "COALESCE(q.frame_count, 0) ASC"
        elif flag == "long":
            order = "COALESCE(q.frame_count, 0) DESC"
        clause = " WHERE " + " AND ".join(where) if where else ""
        with connect(self.settings) as connection:
            rows = connection.execute(
                f"""SELECT e.id, e.episode_key, e.format, e.state, e.created_at,
                           q.frame_count, q.movement_score, q.jerk_score,
                           q.stall_ratio, q.verdict
                    FROM episodes e LEFT JOIN episode_quality q ON q.episode_id = e.id
                    {clause} ORDER BY {order} LIMIT %s""",
                (*params, limit),
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


# Public operations are wrapped once here so every current and future repository
# method reports `catalog_query_duration_seconds` without per-method decorator
# noise. Underscore helpers are internal and stay unwrapped (ADR 0017).
for _name, _method in list(vars(PostgresCatalog).items()):
    if _name.startswith("_") or not callable(_method):
        continue
    setattr(PostgresCatalog, _name, _timed_operation(_method))
del _name, _method
