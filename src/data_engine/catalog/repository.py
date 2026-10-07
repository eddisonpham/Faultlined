"""PostgreSQL repository for vertical-slice jobs, episodes, artifacts, and lineage."""

from __future__ import annotations

import functools
import hashlib
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, cast

from psycopg.types.json import Jsonb

from data_engine.canonical import canonical_json as _canonical_json
from data_engine.catalog.database import connect
from data_engine.config import Settings
from data_engine.curation import (
    EPISODE_FLAGS,
    EPISODE_FRAMES_SQL,
    EPISODE_STATES,
    episode_predicates,
)
from data_engine.jobs.state import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    DEFAULT_MAX_ATTEMPTS,
    ORPHANED_JOB_SECONDS,
    JobState,
)
from data_engine.observability.metrics import RuntimeMetrics
from data_engine.validation.profile import ValidationProfile, profile_hash

#: Advisory-lock key for the one monitor tick. Arbitrary but fixed: any process that
#: wants to be the monitor must use this exact number, and no other lease in the
#: system may reuse it (see ADR 0031).
MONITOR_TICK_LOCK_KEY = 4610555735


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


class SliceNameConflict(ValueError):
    """A curation slice already exists under that name.

    Distinct from `IdempotencyConflict`: the clash is on the slice's own name, not
    on a request key, so it gets its own problem code rather than borrowing the
    idempotency wording.
    """


class InvalidTransition(ValueError):
    """A job state transition is not allowed by the lifecycle contract."""


def canonical_json(value: Any) -> bytes:
    """Re-exported from [data_engine.canonical](../canonical.py); kept here because the
    idempotency request hash has always been imported from this module."""
    return _canonical_json(value)


def _optional_float(value: Any) -> float | None:
    """`None` stays `None`; it means "no clock was supplied", not "zero seconds"."""
    if value is None:
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _zscore(value: float | None, stats: dict[str, Any] | None) -> float:
    # `value is None` is real: an episode can be registered without a frame
    # count in its metadata, and the quality read for it used to raise
    # TypeError mid-page. No length, no claim about length.
    mean = (stats or {}).get("mean")
    std = (stats or {}).get("std")
    if value is None or mean is None or not std:
        return 0.0
    return (float(value) - float(mean)) / float(std)


def _drop_reason_sql(state: str | None, flag: str | None) -> tuple[str, list[Any]]:
    """First-failed-predicate attribution as one SQL CASE, state before flag.

    The labels are data-derived (`state=quarantined`, `verdict=smooth`) rather
    than fixed buckets, because "why did this drop" is answered by the episode's
    own values, not by the filter's vocabulary. `unscored` is its own reason: an
    episode nobody measured is not the same as an episode that measured badly.
    """
    branches: list[str] = []
    params: list[Any] = []
    if state:
        branches.append("WHEN e.state <> %s THEN 'state=' || e.state")
        params.append(state)
    if flag == "jerky":
        branches.append(
            "WHEN q.verdict IS DISTINCT FROM 'jerky' THEN "
            "CASE WHEN q.verdict IS NULL THEN 'unscored' ELSE 'verdict=' || q.verdict END"
        )
    elif flag == "stalled":
        branches.append(
            "WHEN q.stall_ratio IS NULL OR q.stall_ratio < 0.5 THEN "
            "CASE WHEN q.stall_ratio IS NULL THEN 'unscored' ELSE 'stall_ratio<0.5' END"
        )
    return "CASE " + " ".join(branches) + " ELSE 'unattributed' END", params


def _assemble_quality_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-episode quality rows into distributions and outlier lists.

    The length distribution uses each episode's *declared* length, not the number
    of frames its quality analysis happened to see. An episode with no declared
    length contributes to no length statistic rather than being counted as zero -
    it is missing a measurement, not short.
    """
    measured = [row for row in rows if row.get("frame_count") is not None]
    lengths = [int(row["frame_count"]) for row in measured]
    # The population stats are computed once: the z-score of every episode is
    # relative to the same population, so recomputing the mean/std per row made
    # assembly quadratic - ~18 s at a 10k-episode catalog (EXP-0010c), against
    # ~20 ms with the stats hoisted. Same numbers, same rounding, one pass.
    length_stats = {"mean": _mean(lengths), "std": _std(lengths)}
    zscores = {
        str(row["episode_id"]): _zscore(row["frame_count"], length_stats) for row in measured
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

    def _count_by(key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            name = str(row.get(key) or "unknown")
            counts[name] = counts.get(name, 0) + 1
        return counts

    return {
        "episode_count": len(rows),
        "verdicts": verdicts,
        "length": _length_summary(lengths),
        # `jerk_score` is mean(|delta|) / the dimension's own range, averaged
        # over the active dimensions, so it is dimensionless: radians and
        # millimetres land on the same axis. `movement_score` is the same motion
        # in raw units, which is what you want when you ask how far a joint
        # actually travelled and useless when you ask which of two datasets
        # moved more. Both are published; only one of them is comparable.
        "speed_distribution": [
            {
                "episode_id": row["episode_id"],
                "movement_score": float(row["movement_score"]),
                "jerk_score": float(row["jerk_score"]),
                "integrity": row.get("integrity", "unknown"),
                "verdict": row["verdict"],
            }
            for row in rows
        ],
        "integrity": {
            str(value): count
            for value, count in sorted(_count_by("integrity").items(), key=lambda kv: -kv[1])
        },
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
                    for row in measured
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


def _max_timestamp_gap(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Largest gap between consecutive per-step timestamps, over a bounded sample.

    A camera that drops from 30 Hz to 1 Hz produces perfectly valid episodes that
    are useless for training, and nothing else in the pipeline notices. The check
    is deliberately forgiving about metadata shape: episodes whose metadata carries
    no timestamp series simply do not participate, which is the same abstain-if-
    absent rule the feature builder follows everywhere else.
    """
    best: dict[str, Any] | None = None
    for row in rows:
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            continue
        stamps = metadata.get("timestamps") or metadata.get("frame_timestamps")
        if not isinstance(stamps, list) or len(stamps) < 2:
            continue
        values: list[float] = []
        for stamp in stamps:
            if isinstance(stamp, (int, float)):
                values.append(float(stamp))
            elif isinstance(stamp, str):
                try:
                    parsed = datetime.fromisoformat(stamp)
                except ValueError:
                    continue
                values.append(
                    parsed.timestamp() if parsed.tzinfo else parsed.replace(tzinfo=UTC).timestamp()
                )
        if len(values) < 2:
            continue
        gap = max(right - left for left, right in pairwise(values))
        if best is None or gap > best["gap_seconds"]:
            best = {
                "gap_seconds": gap,
                "source_hash": str(row.get("source_hash") or ""),
                "episode_id": str(row.get("id") or ""),
            }
    return best


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

    def reap_orphaned_jobs(self, presumed_dead_seconds: float = ORPHANED_JOB_SECONDS) -> int:
        """Recycle jobs whose worker died mid-run; returns how many were reclaimed.

        A worker that is killed - container restart, OOM, `just stop` - never runs its
        own failure path, so the job stays `running` forever: `claim_job` only selects
        `queued`, and `reap_expired_deadlines` only touches jobs that asked for a
        deadline. Nothing else recovers it, and a submission without `deadline_seconds`
        is stranded silently. This is the crash-recovery counterpart to the retry path.

        The window is a presumed-death timeout rather than a lease. A real lease needs
        the worker to renew it, but the handler is synchronous, so a lease could only be
        set at claim time and would degenerate into the same constant. The window is
        deliberately generous: a false reclaim costs a duplicate run, which the
        content-addressed artifact and build-hash design converges on anyway, while a
        window that is too tight would reclaim healthy long-running work.
        """
        with connect(self.settings) as connection:
            # Two statements, not one CTE: a data-modifying CTE runs concurrently with
            # the main query and cannot see its own effects on the target table, so the
            # second half would never observe the rows the first half just updated.
            recycled = connection.execute(
                """UPDATE jobs
                   SET state = %s, started_at = NULL, finished_at = NULL,
                       error = jsonb_build_object(
                           'type', 'WorkerLost',
                           'message', 'worker presumed dead; job requeued')
                   WHERE state = %s
                     AND started_at < now() - make_interval(secs => %s)
                     AND attempts < max_attempts
                   RETURNING id""",
                (
                    JobState.RETRYING.value,
                    JobState.RUNNING.value,
                    presumed_dead_seconds,
                ),
            ).fetchall()
            if recycled:
                connection.execute(
                    "UPDATE jobs SET state = %s WHERE id = ANY(%s)",
                    (JobState.QUEUED.value, [row["id"] for row in recycled]),
                )
            # An orphan with no retry budget left can never be claimed again, so it
            # would sit in `running` forever - the exact stuck state this reaps.
            connection.execute(
                """UPDATE jobs
                   SET state = %s, finished_at = now(),
                       error = jsonb_build_object(
                           'type', 'WorkerLost',
                           'message', 'worker presumed dead; no attempts remaining')
                   WHERE state = %s
                     AND started_at < now() - make_interval(secs => %s)
                     AND attempts >= max_attempts""",
                (
                    JobState.FAILED.value,
                    JobState.RUNNING.value,
                    presumed_dead_seconds,
                ),
            )
        return len(recycled)

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
                """SELECT e.id, e.episode_key, e.source_hash, e.artifact_hash,
                          e.format, e.state, e.created_at
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
                """SELECT e.id, e.state,
                          (e.metadata->>'frame_count')::bigint AS frame_count,
                          q.frame_count AS analysed_frames,
                          q.movement_score,
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

    def record_build(self, build: Any, *, job_id: str | None = None) -> dict[str, Any]:
        """Persist a build and its membership, idempotently on the content hash.

        Two builds of the same episodes are the same build, so the second write
        is a no-op returning the same row rather than a duplicate. Re-ingesting
        the same selection after a no-op rebuild must not grow the table.
        """
        with connect(self.settings) as connection:
            row = connection.execute(
                """INSERT INTO builds
                       (hash, name, manifest, episode_count, profile_hash, code_commit, job_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (hash) DO UPDATE SET name = EXCLUDED.name
                   RETURNING *""",
                (
                    build.hash,
                    build.name,
                    Jsonb(build.manifest),
                    build.episode_count,
                    build.profile_hash,
                    build.code_commit,
                    job_id,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("build upsert returned no row")
            # Two statements, not two per member. `executemany` pipelines the
            # rows in one round trip, which is the difference between 2 and 2N
            # statements: a 50-episode build issued 100 of them, and a real one
            # would issue 20,000. Same rows, same conflict handling, same
            # transaction - only the round trips changed.
            #
            # Lineage edges as well as the join table: `lineage_edges` is the
            # graph everything else already queries, and a build that exists in
            # only one of the two would be invisible to half the traversals.
            members = list(build.episodes)
            connection.cursor().executemany(
                """INSERT INTO build_episodes
                       (build_hash, episode_id, source_hash, artifact_hash, ordinal)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (build_hash, episode_id) DO NOTHING""",
                [
                    (
                        build.hash,
                        member["episode_id"],
                        member["source_hash"],
                        member["artifact_hash"],
                        ordinal,
                    )
                    for ordinal, member in enumerate(members)
                ],
            )
            connection.cursor().executemany(
                """INSERT INTO lineage_edges (from_type, from_ref, to_type, to_ref, relation)
                   VALUES ('build', %s, 'episode', %s, 'contains')
                   ON CONFLICT DO NOTHING""",
                [(build.hash, member["episode_id"]) for member in members],
            )
        return dict(row)

    def get_build(self, build_id: str) -> dict[str, Any] | None:
        with connect(self.settings) as connection:
            row = connection.execute("SELECT * FROM builds WHERE hash = %s", (build_id,)).fetchone()
        return dict(row) if row else None

    def list_builds(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            rows = connection.execute(
                "SELECT * FROM builds ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def build_episodes(self, build_id: str) -> list[dict[str, Any]]:
        """The forward direction: which episodes this build contains (FR-008)."""
        with connect(self.settings) as connection:
            rows = connection.execute(
                """SELECT episode_id, source_hash, artifact_hash, ordinal
                   FROM build_episodes WHERE build_hash = %s ORDER BY ordinal""",
                (build_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def builds_for_episode(self, episode_id: str) -> list[dict[str, Any]]:
        """The reverse direction: which builds contain this episode (FR-008).

        The question an operator actually asks is "I deleted this episode - what
        did that break?", which is a question about one episode, not one build.
        """
        with connect(self.settings) as connection:
            rows = connection.execute(
                """SELECT b.hash, b.name, b.episode_count, b.profile_hash,
                          b.code_commit, b.job_id, b.created_at
                   FROM build_episodes be
                   JOIN builds b ON b.hash = be.build_hash
                   WHERE be.episode_id = %s
                   ORDER BY b.created_at DESC""",
                (episode_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_episodes(self, episode_ids: list[str]) -> list[dict[str, Any]]:
        """Episodes by id, for a build's selection.

        Order is not preserved: the builder sorts by id, because a manifest whose
        order depends on a database's return order is not reproducible.
        """
        if not episode_ids:
            return []
        with connect(self.settings) as connection:
            rows = connection.execute(
                "SELECT * FROM episodes WHERE id = ANY(%s)", (list(episode_ids),)
            ).fetchall()
        return [dict(row) for row in rows]

    def episode_fingerprint_inputs(self, episode_ids: list[str]) -> list[dict[str, Any]]:
        """The signals a behavioural fingerprint is built from, for a set of episodes (ADR 0032).

        One round trip, and only the columns the descriptor reads. `dims` and `motion_trace`
        are the largest jsonb values this catalog stores, so the projection is deliberate: a
        report over a build of thousands must not drag every episode's full metadata and
        validation history along with two columns it will not look at.

        An episode with no `episode_quality` row is absent rather than defaulted - it has never
        been scored, so it has no fingerprint, and the caller counts what it actually scored.
        """
        if not episode_ids:
            return []
        with connect(self.settings) as connection:
            rows = connection.execute(
                """SELECT e.id, coalesce(e.metadata->>'task', '') AS task,
                          q.verdict, q.judged_dims, q.stall_ratio, q.gap_ratio,
                          q.dims, q.motion_trace
                     FROM episodes e JOIN episode_quality q ON q.episode_id = e.id
                    WHERE e.id = ANY(%s)""",
                (list(episode_ids),),
            ).fetchall()
        return [dict(row) for row in rows]

    def build_coverage_inputs(self, build_hash: str, *, limit: int = 20) -> dict[str, Any]:
        """What a build holds and what its catalog holds, per axis (ADR 0033).

        Two statements, both bounded by the number of axis values rather than by the number of
        episodes: the first expands four persisted attributes onto one row per member per axis and
        returns the top `limit` build values plus the catalog-only values of the gap axes; the
        second lists the vocabulary entries with no episode in this build, which is where the
        *task* gaps come from (a label with no episode anywhere is still a gap).

        The window counts travel with the capped rows, so a truncated report can still say how many
        values were left out rather than presenting the sample as the whole. Nothing is stored: the
        report is a query over facts that already exist, so it cannot drift from them.
        """
        with connect(self.settings) as connection:
            values = connection.execute(
                """WITH scoped AS MATERIALIZED (
                       SELECT 'build'::text AS scope, e.id,
                              coalesce(v.preferred_label,
                                       nullif(btrim(e.metadata->>'task'), ''),
                                       '(no task)') AS task,
                              coalesce(nullif(btrim(e.metadata->>'robot'), ''),
                                       nullif(btrim(e.metadata->>'robot_type'), ''),
                                       '(no robot)') AS robot,
                              e.format AS format,
                              coalesce(q.verdict, '(unscored)') AS verdict
                         FROM build_episodes be
                         JOIN episodes e ON e.id = be.episode_id
                         LEFT JOIN task_vocabulary_mappings m
                                ON m.task_string = e.metadata->>'task'
                         LEFT JOIN task_vocabulary_entries v ON v.id = m.entry_id
                         LEFT JOIN episode_quality q ON q.episode_id = e.id
                        WHERE be.build_hash = %s
                       UNION ALL
                       SELECT 'catalog', e.id,
                              coalesce(v.preferred_label,
                                       nullif(btrim(e.metadata->>'task'), ''),
                                       '(no task)') AS task,
                              coalesce(nullif(btrim(e.metadata->>'robot'), ''),
                                       nullif(btrim(e.metadata->>'robot_type'), ''),
                                       '(no robot)') AS robot,
                              e.format AS format,
                              coalesce(q.verdict, '(unscored)') AS verdict
                         FROM episodes e
                         LEFT JOIN task_vocabulary_mappings m
                                ON m.task_string = e.metadata->>'task'
                         LEFT JOIN task_vocabulary_entries v ON v.id = m.entry_id
                         LEFT JOIN episode_quality q ON q.episode_id = e.id
                   ), per_value AS (
                       SELECT scope, axis, value, count(*) AS n
                         FROM (SELECT scope, id, a.axis, a.value
                                 FROM scoped
                                 CROSS JOIN LATERAL (VALUES ('task', task),
                                                            ('robot', robot),
                                                            ('format', format),
                                                            ('verdict', verdict))
                                            AS a(axis, value)) AS expanded
                        GROUP BY scope, axis, value
                   ), joined AS (
                       SELECT coalesce(b.axis, c.axis) AS axis,
                              coalesce(b.value, c.value) AS value,
                              coalesce(b.n, 0) AS build_count,
                              coalesce(c.n, 0) AS catalog_count
                         FROM (SELECT * FROM per_value WHERE scope = 'build') AS b
                         FULL JOIN (SELECT * FROM per_value WHERE scope = 'catalog') AS c
                                ON b.axis = c.axis AND b.value = c.value
                   ), ranked AS (
                       SELECT axis, value, build_count, catalog_count,
                              count(*) FILTER (WHERE build_count > 0) OVER (PARTITION BY axis)
                                  AS build_distinct,
                              count(*) FILTER (WHERE catalog_count > 0) OVER (PARTITION BY axis)
                                  AS catalog_distinct,
                              count(*) FILTER (WHERE build_count = 0) OVER (PARTITION BY axis)
                                  AS gap_total,
                              sum(build_count) OVER (PARTITION BY axis)::bigint AS build_total,
                              sum(catalog_count) OVER (PARTITION BY axis)::bigint AS catalog_total,
                              row_number() OVER (PARTITION BY axis
                                                 ORDER BY build_count DESC, value) AS build_rank,
                              row_number() OVER (PARTITION BY axis ORDER BY value) AS value_rank
                         FROM joined
                   )
                   SELECT * FROM ranked
                    WHERE (build_count > 0 AND build_rank <= %s)
                       OR (build_count = 0 AND catalog_count > 0 AND axis <> 'task'
                           AND value_rank <= %s)
                    ORDER BY axis, build_count DESC, value""",
                (build_hash, limit, limit),
            ).fetchall()
            vocabulary = connection.execute(
                """WITH entries AS (
                       SELECT id, preferred_label FROM task_vocabulary_entries
                   ), gaps AS (
                       SELECT v.id, v.preferred_label AS label,
                              (SELECT count(*)
                                 FROM episodes e
                                 JOIN task_vocabulary_mappings m
                                   ON m.task_string = e.metadata->>'task'
                                WHERE m.entry_id = v.id) AS catalog_episodes
                         FROM entries v
                        WHERE NOT EXISTS (
                                  SELECT 1 FROM build_episodes be
                                    JOIN episodes e ON e.id = be.episode_id
                                    JOIN task_vocabulary_mappings m
                                      ON m.task_string = e.metadata->>'task'
                                   WHERE be.build_hash = %s AND m.entry_id = v.id)
                   )
                   SELECT (SELECT count(*) FROM entries) AS total,
                          (SELECT count(*) FROM gaps) AS missing_total,
                          (SELECT coalesce(jsonb_agg(jsonb_build_object(
                                      'id', id, 'label', label,
                                      'catalog_episodes', catalog_episodes)
                                      ORDER BY label), '[]'::jsonb)
                             FROM (SELECT * FROM gaps ORDER BY label LIMIT %s) AS capped)
                            AS gaps""",
                (build_hash, limit),
            ).fetchone()
        rows = [dict(row) for row in values]
        entry = dict(vocabulary) if vocabulary else {}
        entry.setdefault("total", 0)
        entry.setdefault("missing_total", 0)
        entry.setdefault("gaps", [])
        return {
            "episode_count": rows[0]["build_total"] if rows else 0,
            "catalog_size": rows[0]["catalog_total"] if rows else 0,
            "values": rows,
            "vocabulary": entry,
        }

    def record_episode_quality(self, episode_id: str, quality: dict[str, Any]) -> dict[str, Any]:
        """Persist the motion-quality summary computed at ingest (ADR 0018).

        A content-addressed episode recomputes identical signals on re-ingest, so
        the row is replaced rather than accumulated.
        """
        with connect(self.settings) as connection:
            row = connection.execute(
                """INSERT INTO episode_quality
                       (episode_id, frame_count, movement_score, jerk_score,
                        stall_ratio, verdict, dims, nonfinite, max_gap_seconds,
                        gap_ratio, integrity, worst_verdict, worst_dim, judged_dims,
                        motion_trace)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (episode_id) DO UPDATE
                       SET frame_count = EXCLUDED.frame_count,
                           movement_score = EXCLUDED.movement_score,
                           jerk_score = EXCLUDED.jerk_score,
                           stall_ratio = EXCLUDED.stall_ratio,
                           verdict = EXCLUDED.verdict,
                           dims = EXCLUDED.dims,
                           nonfinite = EXCLUDED.nonfinite,
                           max_gap_seconds = EXCLUDED.max_gap_seconds,
                           gap_ratio = EXCLUDED.gap_ratio,
                           integrity = EXCLUDED.integrity,
                           worst_verdict = EXCLUDED.worst_verdict,
                           worst_dim = EXCLUDED.worst_dim,
                           judged_dims = EXCLUDED.judged_dims,
                           motion_trace = EXCLUDED.motion_trace,
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
                    int(quality.get("nonfinite") or 0),
                    _optional_float(quality.get("max_gap_seconds")),
                    float(quality.get("gap_ratio") or 0.0),
                    str(quality.get("integrity") or "unknown"),
                    str(quality.get("worst_verdict") or "unknown"),
                    quality.get("worst_dim"),
                    int(quality.get("judged_dims") or 0),
                    Jsonb(quality.get("motion_trace") or []),
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
                """SELECT q.episode_id, q.frame_count AS analysed_frames,
                          q.movement_score, q.jerk_score,
                          q.stall_ratio, q.verdict, q.dims, q.computed_at,
                          q.nonfinite, q.max_gap_seconds, q.gap_ratio,
                          q.integrity, q.worst_verdict, q.worst_dim, q.judged_dims,
                          q.motion_trace,
                          (e.metadata->>'frame_count')::bigint AS frame_count
                   FROM episode_quality q JOIN episodes e ON e.id = q.episode_id
                   WHERE q.episode_id = %s""",
                (episode_id,),
            ).fetchone()
            # The z-score asks "how long is this episode relative to the others",
            # so both sides are episode lengths. Scoring against analysed-sample
            # counts would compare a sample against the population.
            stats = connection.execute(
                """SELECT avg((metadata->>'frame_count')::bigint) AS mean,
                          stddev_samp((metadata->>'frame_count')::bigint) AS std
                   FROM episodes WHERE metadata ? 'frame_count'"""
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
                """SELECT q.episode_id, e.episode_key,
                          (e.metadata->>'frame_count')::bigint AS frame_count,
                          q.frame_count AS analysed_frames,
                          q.movement_score,
                          q.jerk_score, q.stall_ratio, q.verdict, q.dims,
                          q.integrity, q.max_gap_seconds, q.gap_ratio,
                          q.worst_verdict, q.worst_dim, q.judged_dims
                   FROM episode_quality q JOIN episodes e ON e.id = q.episode_id
                   ORDER BY q.movement_score DESC"""
            ).fetchall()
        return _assemble_quality_summary([dict(row) for row in rows])

    def list_episodes(
        self,
        *,
        limit: int = 50,
        state: str | None = None,
        flag: str | None = None,
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Episode page with quality columns for the Episodes UI.

        Flags are curation views: `jerky`/`stalled` filter on quality signals and
        rank by the signal, `short`/`long` reorder by frame count so the tails of
        the length distribution surface first. Default is newest first.

        With ``before`` the page is cursor-based on ``created_at`` and ordered
        newest first regardless of flag ranking: a signal ranking cannot page
        consistently (page 2 would re-include ranked rows the cursor filter
        admits), and a download needs every matching row exactly once.
        """
        where, params, order = episode_predicates(state, flag, prefix="e.")
        if before is not None:
            where = [*where, "e.created_at < %s"]
            params = [*params, before]
            order = "e.created_at DESC"
        clause = " WHERE " + " AND ".join(where) if where else ""
        frames = EPISODE_FRAMES_SQL.format(prefix="e.")
        with connect(self.settings) as connection:
            rows = connection.execute(
                f"""SELECT e.id, e.episode_key, e.source_hash, e.artifact_hash,
                           e.format, e.state, e.created_at,
                           {frames} AS frame_count,
                           q.frame_count AS analysed_frames,
                           q.movement_score, q.jerk_score,
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

    def count_jobs_by_state(self) -> dict[str, int]:
        """Queue depth per state in one round trip.

        The status and metrics models need every state; asking for them one query
        at a time cost 8 connections and 8 scans per request - 588 ms of the 599 ms
        `/api/v1/metrics` took at 10 rps (EXP-0010e). One GROUP BY is the same
        numbers in one scan. States with no rows are reported as 0 rather than
        omitted, because "not present" and "zero deep" are the same fact to a
        queue-depth gauge but not to a JSON consumer indexing by state.
        """
        with connect(self.settings) as connection:
            rows = connection.execute(
                "SELECT state, count(*) AS total FROM jobs GROUP BY state"
            ).fetchall()
        counts = {state.value: 0 for state in JobState}
        for row in rows:
            counts[str(row["state"])] = cast(int, row["total"])
        return counts

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

    # ---- episode slices (curated build-ready layer) ----

    def list_slices(
        self, *, limit: int = 50, before: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Newest-first saved slices; bounded and cursor-based."""
        params: list[Any] = []
        clause = ""
        if before is not None:
            clause = "WHERE s.created_at < %s"
            params.append(before)
        params.append(limit)
        with connect(self.settings) as connection:
            rows = connection.execute(
                f"""SELECT s.id, s.name, s.notes, s.filter_config,
                          s.created_at, s.updated_at,
                          coalesce(
                              (SELECT count(*) FROM slice_memberships sm
                               WHERE sm.slice_id = s.id),
                              0
                          ) AS member_count
                   FROM episode_slices s
                   {clause}
                   ORDER BY s.created_at DESC, s.id DESC
                   LIMIT %s""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_slice(self, slice_id: str) -> dict[str, Any] | None:
        with connect(self.settings) as connection:
            row = connection.execute(
                """SELECT s.id, s.name, s.notes, s.filter_config,
                          s.created_at, s.updated_at,
                          coalesce(
                              (SELECT count(*) FROM slice_memberships sm
                               WHERE sm.slice_id = s.id),
                              0
                          ) AS member_count
                   FROM episode_slices s WHERE s.id = %s""",
                (slice_id,),
            ).fetchone()
        return dict(row) if row else None

    def register_slice(
        self,
        *,
        name: str,
        notes: str = "",
        filter_config: dict[str, Any] | None = None,
        slice_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or refresh a named slice; idempotent on name.

        Membership is recomputed on the next manifest read, not here, so the create
        path stays fast and the slice is always correct.
        """
        config = dict(filter_config or {})
        id_ = slice_id or str(uuid.uuid4())
        with connect(self.settings) as connection:
            existing = connection.execute(
                "SELECT id FROM episode_slices WHERE name = %s", (name,)
            ).fetchone()
            if existing is not None:
                raise SliceNameConflict(f"slice name {name!r} already exists")
            row = connection.execute(
                """INSERT INTO episode_slices
                       (id, name, notes, filter_config)
                   VALUES (%s, %s, %s, %s)
                   RETURNING *""",
                (id_, name, notes, Jsonb(config)),
            ).fetchone()
            if row is None:
                raise RuntimeError("slice insert returned no row")
        return dict(row)

    def update_slice(
        self,
        slice_id: str,
        *,
        name: str | None = None,
        notes: str | None = None,
        filter_config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with connect(self.settings) as connection:
            existing = connection.execute(
                "SELECT * FROM episode_slices WHERE id = %s", (slice_id,)
            ).fetchone()
            if existing is None:
                return None
            updates: list[str] = ["updated_at = now()"]
            params: list[Any] = []
            if name is not None:
                updates.append("name = %s")
                params.append(name)
            if notes is not None:
                updates.append("notes = %s")
                params.append(notes)
            if filter_config is not None:
                updates.append("filter_config = %s")
                params.append(Jsonb(filter_config))
            params.append(slice_id)
            connection.execute(
                f"UPDATE episode_slices SET {', '.join(updates)} WHERE id = %s",
                params,
            )
        return self.get_slice(slice_id)

    def delete_slice(self, slice_id: str) -> bool:
        with connect(self.settings) as connection:
            row = connection.execute(
                "DELETE FROM episode_slices WHERE id = %s RETURNING id",
                (slice_id,),
            ).fetchone()
        return bool(row)

    def slice_manifest(self, slice_id: str, *, limit: int = 100) -> dict[str, Any] | None:
        """Manifest for a saved slice: what episodes it includes, with identity.

        Membership is recomputed from the filter config at read time so the slice is
        always correct; the membership row is only an audit trail of inclusion.
        """
        slice_ = self.get_slice(slice_id)
        if slice_ is None:
            return None
        config = dict(slice_.get("filter_config") or {})
        state = config.get("state")
        flag = config.get("flag")
        if state is not None and state not in EPISODE_STATES:
            return None
        if flag is not None and flag not in EPISODE_FLAGS:
            return None
        items = self._slice_members(slice_id, state, flag, limit)
        return {
            "slice_id": slice_id,
            "name": slice_.get("name"),
            "filters": {"state": state, "flag": flag},
            "generated_at": datetime.now(UTC).isoformat(),
            "count": len(items),
            "items": items,
        }

    def _slice_members(
        self,
        slice_id: str,
        state: str | None,
        flag: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Recompute membership from the filter config and register it in the audit trail."""
        rows = self.list_episodes(limit=limit, state=state, flag=flag)
        # One statement, not one per member. Membership is recomputed on every
        # read by design, which means this write is on the hot path of every
        # manifest fetch: a 20-episode slice issued 67 statements.
        with connect(self.settings) as connection:
            connection.cursor().executemany(
                """INSERT INTO slice_memberships (slice_id, episode_id)
                   VALUES (%s, %s)
                   ON CONFLICT (slice_id, episode_id) DO NOTHING""",
                [(slice_id, row["id"]) for row in rows],
            )
        return rows

    def slice_impact(self, slice_id: str) -> dict[str, Any] | None:
        """What the slice keeps and drops versus the whole dataset, and why.

        The kept/dropped split is the *same predicate* `episode_predicates` builds
        for the manifest - one vocabulary, one truth - so this view cannot
        disagree with the manifest it explains. Drop reasons attribute each
        excluded episode to the first predicate it fails (state before flag);
        `short`/`long` are orderings, not filters, and the result says so instead
        of manufacturing drops. All aggregation is in SQL: a view that fetched
        every episode to compute a median would be O(dataset) on a detail page.
        """
        slice_ = self.get_slice(slice_id)
        if slice_ is None:
            return None
        config = dict(slice_.get("filter_config") or {})
        state = config.get("state")
        flag = config.get("flag")
        if state is not None and state not in EPISODE_STATES:
            return None
        if flag is not None and flag not in EPISODE_FLAGS:
            return None
        where, params, _ = episode_predicates(state, flag, prefix="e.")
        match = " AND ".join(where) if where else "TRUE"
        frames = EPISODE_FRAMES_SQL.format(prefix="e.")

        def side(clause: str, side_params: list[Any]) -> dict[str, Any]:
            with connect(self.settings) as connection:
                row = connection.execute(
                    f"""SELECT count(*) AS total,
                               count(q.jerk_score) AS scored,
                               percentile_cont(0.5) WITHIN GROUP
                                   (ORDER BY q.jerk_score) AS median_jerk,
                               percentile_cont(0.5) WITHIN GROUP
                                   (ORDER BY q.stall_ratio) AS median_stall,
                               percentile_cont(0.5) WITHIN GROUP
                                   (ORDER BY {frames}) AS median_frames,
                               count(*) FILTER (WHERE q.integrity = 'gapped') AS gapped
                        FROM episodes e LEFT JOIN episode_quality q ON q.episode_id = e.id
                        WHERE {clause}""",
                    side_params,
                ).fetchone()
            assert row is not None
            return {
                "count": cast(int, row["total"]),
                "scored": cast(int, row["scored"]),
                "median_jerk_score": _optional_float(row["median_jerk"]),
                "median_stall_ratio": _optional_float(row["median_stall"]),
                "median_frames": _optional_float(row["median_frames"]),
                "gapped": cast(int, row["gapped"]),
            }

        kept = side(match, params)
        dropped = side(f"NOT ({match})", params)
        reasons: list[dict[str, Any]] = []
        if where:
            reason_sql, reason_params = _drop_reason_sql(state, flag)
            with connect(self.settings) as connection:
                rows = connection.execute(
                    f"""SELECT {reason_sql} AS reason, count(*) AS total
                        FROM episodes e LEFT JOIN episode_quality q ON q.episode_id = e.id
                        WHERE NOT ({match})
                        GROUP BY 1 ORDER BY total DESC, reason""",
                    [*reason_params, *params],
                ).fetchall()
            reasons = [
                {"reason": str(row["reason"]), "count": cast(int, row["total"])} for row in rows
            ]
        return {
            "slice_id": slice_id,
            "name": slice_.get("name"),
            "filters": {"state": state, "flag": flag},
            "reorders_only": not where,
            "dataset": {
                "episodes": kept["count"] + dropped["count"],
                "scored": kept["scored"] + dropped["scored"],
            },
            "kept": kept,
            "dropped": dropped,
            "drop_reasons": reasons,
        }

    # ---- validation failures read view ----

    def failure_summary(self) -> dict[str, Any] | None:
        """Aggregate of what is failing across episodes and profiles; read-only."""
        with connect(self.settings) as connection:
            total = connection.execute(
                "SELECT count(*) AS total FROM episodes WHERE state = 'quarantined'"
            ).fetchone()
            evaluated = connection.execute(
                "SELECT count(DISTINCT episode_id) AS evaluated FROM validation_results"
            ).fetchone()
            rows = connection.execute("""SELECT reason_codes FROM validation_results""").fetchall()
            by_profile = connection.execute(
                """SELECT profile_name, count(*) AS failed
                   FROM validation_results vr
                   JOIN episodes e ON e.id = vr.episode_id
                   WHERE NOT vr.passed
                   GROUP BY profile_name"""
            ).fetchall()
            by_format = connection.execute(
                """SELECT e.format, count(*) AS failed
                   FROM validation_results vr
                   JOIN episodes e ON e.id = vr.episode_id
                   WHERE NOT vr.passed
                   GROUP BY e.format"""
            ).fetchall()
        if rows is None or total is None or evaluated is None:
            return None
        reason_codes: dict[str, int] = {}
        for row in rows:
            for code in cast(list[Any], row["reason_codes"] or []):
                reason_codes[str(code)] = reason_codes.get(str(code), 0) + 1
        return {
            "reason_codes": reason_codes,
            "by_profile": [dict(row) for row in by_profile],
            "by_format": [dict(row) for row in by_format],
            "quarantined_count": int(cast(int, total["total"])),
            "episodes_evaluated": int(cast(int, evaluated["evaluated"])),
        }

    def failing_episodes(
        self, *, limit: int = 50, before: datetime | None = None, reason_code: str | None = None
    ) -> list[dict[str, Any]]:
        """Quarantined episodes with their current result's reason codes and violations."""
        clauses: list[str] = ["e.state = 'quarantined'"]
        params: list[Any] = []
        if reason_code is not None:
            clauses.append("vr.reason_codes @> %s")
            params.append(Jsonb([reason_code]))
        if before is not None:
            clauses.append("e.created_at < %s")
            params.append(before)
        where = f"WHERE {' AND '.join(clauses)}"
        params.append(limit)
        with connect(self.settings) as connection:
            rows = connection.execute(
                f"""SELECT e.id, e.episode_key, e.format, e.state, e.created_at,
                          q.frame_count, q.movement_score, q.jerk_score,
                          q.stall_ratio, q.verdict,
                          vr.profile_name, vr.profile_version, vr.passed,
                          vr.reason_codes, vr.violations
                   FROM episodes e
                   LEFT JOIN episode_quality q ON q.episode_id = e.id
                   JOIN validation_results vr
                     ON vr.episode_id = e.id AND NOT vr.passed
                   {where}
                   ORDER BY e.created_at DESC, e.id DESC
                   LIMIT %s""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- monitoring notifier (ADR 0020) ----

    def monitoring_snapshot(
        self, *, now: datetime | None = None, clock_sample: int = 200
    ) -> dict[str, Any]:
        """One round trip of catalog state for one evaluation tick.

        The feature builder is pure, so everything it needs is gathered here and
        handed over as a plain mapping. Gathering it in a single query matters:
        seven separate count queries per tick would make the monitor the most
        expensive thing in the process.
        """
        reference = now or datetime.now(UTC)
        with connect(self.settings) as connection:
            depth_rows = connection.execute(
                "SELECT state, count(*) AS total FROM jobs GROUP BY state"
            ).fetchall()
            oldest = connection.execute(
                """SELECT extract(epoch FROM (%(now)s - min(created_at))) AS age
                   FROM jobs WHERE state = 'queued'""",
                {"now": reference},
            ).fetchone()
            running_rows = connection.execute(
                """SELECT id, type,
                          extract(epoch FROM (%(now)s - coalesce(started_at, created_at))) AS age,
                          (deadline_at IS NOT NULL AND deadline_at < %(now)s) AS deadline_passed
                   FROM jobs
                   WHERE state IN ('running', 'cancel_requested', 'retrying')
                   ORDER BY coalesce(started_at, created_at)
                   LIMIT 50""",
                {"now": reference},
            ).fetchall()
            episodes = connection.execute(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE state = 'quarantined') AS quarantined
                   FROM episodes"""
            ).fetchone()
            verdicts = connection.execute(
                "SELECT verdict, count(*) AS total FROM episode_quality GROUP BY verdict"
            ).fetchall()
            quality = connection.execute(
                """SELECT avg(q.movement_score) AS movement, avg(q.jerk_score) AS jerk,
                          avg(q.stall_ratio) AS stall,
                          percentile_cont(0.5) WITHIN GROUP (
                              ORDER BY (e.metadata->>'frame_count')::bigint) AS frames
                   FROM episode_quality q JOIN episodes e ON e.id = q.episode_id
                   WHERE e.metadata ? 'frame_count'"""
            ).fetchone()
            recent = connection.execute(
                """SELECT id, source_hash, metadata FROM episodes
                   ORDER BY created_at DESC LIMIT %s""",
                (clock_sample,),
            ).fetchall()

        snapshot: dict[str, Any] = {
            "queue_depth": {str(row["state"]): int(cast(int, row["total"])) for row in depth_rows},
            "running": [
                {
                    "id": str(row["id"]),
                    "job_type": str(row["type"]),
                    "age_seconds": float(cast(float, row["age"] or 0.0)),
                    "deadline_passed": bool(row["deadline_passed"]),
                }
                for row in running_rows
            ],
            "verdict_rates": {
                str(row["verdict"]): int(cast(int, row["total"])) for row in verdicts
            },
        }
        if oldest is not None and oldest["age"] is not None:
            snapshot["oldest_queued_age_seconds"] = float(cast(float, oldest["age"]))
        if episodes is not None and int(cast(int, episodes["total"]) or 0) > 0:
            snapshot["quarantine_rate"] = int(cast(int, episodes["quarantined"]) or 0) / int(
                cast(int, episodes["total"])
            )
        if quality is not None:
            for key, column in (
                ("movement_mean", "movement"),
                ("jerk_mean", "jerk"),
                ("stall_mean", "stall"),
                ("frame_count_median", "frames"),
            ):
                if quality[column] is not None:
                    snapshot[key] = float(cast(float, quality[column]))
        gap = _max_timestamp_gap(recent)
        if gap is not None:
            snapshot["max_episode_timestamp_gap_seconds"] = gap["gap_seconds"]
            snapshot["timestamp_gap_source"] = gap["source_hash"]
            snapshot["timestamp_gap_scope"] = gap["episode_id"]
        return snapshot

    @contextmanager
    def monitor_lease(self) -> Iterator[bool]:
        """Become this catalog's monitor ticker for the duration of the block.

        Yields ``True`` when this process holds the lease and ``False`` when another
        worker is already ticking. The monitor's tick is not idempotent - it bumps an
        incident's occurrence count and observes the baseline book - and running N
        workers is a supported configuration, so without a lease the counts scale
        with the number of workers rather than with the number of faults. The lock is
        session-scoped: it is released when this connection closes, so a worker killed
        mid-tick cannot hold it against the rest of the pool.
        """
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (MONITOR_TICK_LOCK_KEY,)
            ).fetchone()
            acquired = bool(row and row["acquired"])
            try:
                yield acquired
            finally:
                if acquired:
                    connection.execute("SELECT pg_advisory_unlock(%s)", (MONITOR_TICK_LOCK_KEY,))

    def upsert_incident(
        self,
        *,
        incident_id: str,
        fingerprint: str,
        label: str,
        severity: str,
        notify_class: str,
        scope: str,
        summary: str,
        evidence: dict[str, Any],
        feature_schema_version: int,
        seen_at: datetime,
    ) -> dict[str, Any]:
        """Create or bump one incident, atomically.

        The partial unique index on ``(fingerprint) WHERE status <> 'resolved'`` is
        the dedup guarantee, so the conflict target is that index rather than a
        read-then-write in Python. A crash between the triage decision and the
        insert cannot therefore open a second row for the same fault.
        """
        with connect(self.settings) as connection:
            row = connection.execute(
                """INSERT INTO monitor_incidents
                       (id, fingerprint, label, severity, notify_class, scope, summary,
                        evidence, status, occurrence_count, feature_schema_version,
                        first_seen, last_seen)
                   VALUES (%(id)s, %(fingerprint)s, %(label)s, %(severity)s, %(notify)s,
                           %(scope)s, %(summary)s, %(evidence)s, 'open', 1, %(schema)s,
                           %(seen)s, %(seen)s)
                   ON CONFLICT (fingerprint) WHERE status <> 'resolved' DO UPDATE
                       SET occurrence_count = monitor_incidents.occurrence_count + 1,
                           last_seen = EXCLUDED.last_seen,
                           evidence = EXCLUDED.evidence,
                           summary = EXCLUDED.summary,
                           severity = CASE
                               WHEN monitor_incidents.severity = 'critical'
                                    AND EXCLUDED.severity <> 'critical'
                               THEN monitor_incidents.severity
                               ELSE EXCLUDED.severity
                           END
                   RETURNING *""",
                {
                    "id": incident_id,
                    "fingerprint": fingerprint,
                    "label": label,
                    "severity": severity,
                    "notify": notify_class,
                    "scope": scope,
                    "summary": summary,
                    "evidence": Jsonb(evidence),
                    "schema": feature_schema_version,
                    "seen": seen_at,
                },
            ).fetchone()
        if row is None:
            raise RuntimeError("incident upsert returned no row")
        return dict(row)

    def list_incidents(
        self,
        *,
        severity: str | None = None,
        status: str | None = None,
        label: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first incident page, cursor-based on ``last_seen``."""
        clauses: list[str] = []
        params: list[Any] = []
        if severity is not None:
            clauses.append("severity = %s")
            params.append(severity)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if label is not None:
            clauses.append("label = %s")
            params.append(label)
        if before is not None:
            clauses.append("last_seen < %s")
            params.append(before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with connect(self.settings) as connection:
            rows = connection.execute(
                f"""SELECT id, fingerprint, label, severity, notify_class, scope, summary,
                           evidence, status, occurrence_count, feature_schema_version,
                           first_seen, last_seen, acknowledged_at, resolved_at
                    FROM monitor_incidents {where}
                    ORDER BY last_seen DESC, id DESC LIMIT %s""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT * FROM monitor_incidents WHERE id = %s", (incident_id,)
            ).fetchone()
        return dict(row) if row else None

    def unresolved_incidents(self) -> list[dict[str, Any]]:
        """Fingerprints triage needs to reason about dedup, cooldown, and the budget."""
        with connect(self.settings) as connection:
            rows = connection.execute(
                """SELECT id, fingerprint, status, last_seen, occurrence_count
                   FROM monitor_incidents WHERE status <> 'resolved'"""
            ).fetchall()
        return [dict(row) for row in rows]

    def incidents_opened_since(self, since: datetime) -> int:
        """Distinct incidents opened in the budget window.

        Counts rows by ``first_seen`` and deliberately *not* by ``occurrence_count``:
        a fault that recurs twenty times is one thing that went wrong, and counting
        its bumps would mean a long-running incident silently refunds its own budget
        the more it persists — which is precisely the fault a budget exists to cap.
        """
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT count(*) AS total FROM monitor_incidents WHERE first_seen >= %s",
                (since,),
            ).fetchone()
        return int(cast(int, row["total"])) if row else 0

    def set_incident_status(self, incident_id: str, status: str) -> dict[str, Any] | None:
        """Acknowledge or resolve; both are append-only facts, never deletions."""
        column = "acknowledged_at" if status == "acknowledged" else "resolved_at"
        with connect(self.settings) as connection:
            row = connection.execute(
                f"""UPDATE monitor_incidents
                    SET status = %s, {column} = COALESCE({column}, now())
                    WHERE id = %s AND status <> 'resolved'
                    RETURNING *""",
                (status, incident_id),
            ).fetchone()
        return dict(row) if row else None

    def incident_summary(self) -> dict[str, Any]:
        """Queue shape for the header and the monitor's own health readout."""
        with connect(self.settings) as connection:
            by_status = connection.execute(
                "SELECT status, count(*) AS total FROM monitor_incidents GROUP BY status"
            ).fetchall()
            by_severity = connection.execute(
                """SELECT severity, count(*) AS total FROM monitor_incidents
                   WHERE status <> 'resolved' GROUP BY severity"""
            ).fetchall()
            by_label = connection.execute(
                """SELECT label, count(*) AS total FROM monitor_incidents
                   WHERE status <> 'resolved' GROUP BY label ORDER BY total DESC"""
            ).fetchall()
            notify_rows = connection.execute(
                """SELECT count(*) AS total FROM monitor_incidents
                   WHERE status <> 'resolved' AND notify_class = 'notify'"""
            ).fetchone()
        return {
            "by_status": {str(row["status"]): int(cast(int, row["total"])) for row in by_status},
            "by_severity": {
                str(row["severity"]): int(cast(int, row["total"])) for row in by_severity
            },
            "by_label": {str(row["label"]): int(cast(int, row["total"])) for row in by_label},
            "notify_open": int(cast(int, notify_rows["total"])) if notify_rows else 0,
        }

    def load_baselines(self) -> list[dict[str, Any]]:
        with connect(self.settings) as connection:
            rows = connection.execute(
                """SELECT feature, scope, samples, center, breached, held_since
                   FROM monitor_baselines"""
            ).fetchall()
        return [dict(row) for row in rows]

    def save_baselines(self, rows: Sequence[dict[str, Any]]) -> int:
        """Upsert every control limit touched by a tick.

        One statement for the whole set: at ~30 scopes per tick this is a single
        round trip, and a partially-written baseline set is worse than a stale one.
        """
        if not rows:
            return 0
        payload = [
            (
                row["feature"],
                row.get("scope", ""),
                Jsonb(list(row.get("samples") or [])),
                float(row.get("center") or 0.0),
                bool(row.get("breached")),
                row.get("held_since"),
            )
            for row in rows
        ]
        with connect(self.settings) as connection:
            connection.execute(
                """INSERT INTO monitor_baselines
                       (feature, scope, samples, center, breached, held_since, updated_at)
                   SELECT *, now() FROM unnest(
                       %s::text[], %s::text[], %s::jsonb[],
                       %s::double precision[], %s::boolean[], %s::double precision[])
                   ON CONFLICT (feature, scope) DO UPDATE
                       SET samples = EXCLUDED.samples,
                           center = EXCLUDED.center,
                           breached = EXCLUDED.breached,
                           held_since = EXCLUDED.held_since,
                           updated_at = now()""",
                (
                    [item[0] for item in payload],
                    [item[1] for item in payload],
                    [item[2] for item in payload],
                    [item[3] for item in payload],
                    [item[4] for item in payload],
                    [item[5] for item in payload],
                ),
            )
        return len(payload)

    def register_contract(self, job_id: str, expectation: dict[str, Any]) -> dict[str, Any]:
        """Declare a completion contract for a run, idempotently per job."""
        with connect(self.settings) as connection:
            row = connection.execute(
                """INSERT INTO job_contracts
                       (job_id, expected_episodes, expected_valid_fraction,
                        max_duration_seconds, deadline_at)
                   VALUES (%(job)s, %(episodes)s, %(fraction)s, %(duration)s, %(deadline)s)
                   ON CONFLICT (job_id) DO UPDATE
                       SET expected_episodes = EXCLUDED.expected_episodes,
                           expected_valid_fraction = EXCLUDED.expected_valid_fraction,
                           max_duration_seconds = EXCLUDED.max_duration_seconds,
                           deadline_at = EXCLUDED.deadline_at,
                           outcome = 'pending',
                           observed = '{}'::jsonb,
                           updated_at = now()
                   RETURNING *""",
                {
                    "job": job_id,
                    "episodes": expectation.get("expected_episodes"),
                    "fraction": expectation.get("expected_valid_fraction"),
                    "duration": expectation.get("max_duration_seconds"),
                    "deadline": expectation.get("deadline_at"),
                },
            ).fetchone()
        if row is None:
            raise RuntimeError("contract upsert returned no row")
        return dict(row)

    def get_contract(self, job_id: str) -> dict[str, Any] | None:
        with connect(self.settings) as connection:
            row = connection.execute(
                "SELECT * FROM job_contracts WHERE job_id = %s", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_contracts(
        self, *, outcome: str | None = None, limit: int = 50, before: datetime | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if outcome is not None:
            clauses.append("c.outcome = %s")
            params.append(outcome)
        if before is not None:
            clauses.append("c.created_at < %s")
            params.append(before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with connect(self.settings) as connection:
            rows = connection.execute(
                f"""SELECT c.*, j.state AS job_state, j.type AS job_type
                    FROM job_contracts c JOIN jobs j ON j.id = c.job_id
                    {where}
                    ORDER BY c.created_at DESC, c.job_id DESC LIMIT %s""",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_contract_outcomes(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Contracts still pending, with the observed counts needed to evaluate them.

        Episode counts come from the lineage edge written at ingest, so a contract
        measures the same episodes the build will contain.
        """
        produced = """(SELECT count(*) FROM lineage_edges l
                            JOIN episodes e ON e.id = l.from_ref
                           WHERE l.from_type = 'episode' AND l.to_type = 'job'
                             AND l.to_ref = c.job_id)"""
        valid = """(SELECT count(*) FROM lineage_edges l
                            JOIN episodes e ON e.id = l.from_ref
                           WHERE l.from_type = 'episode' AND l.to_type = 'job'
                             AND l.to_ref = c.job_id AND e.state = 'valid')"""
        with connect(self.settings) as connection:
            rows = connection.execute(
                f"""SELECT c.*, j.state AS job_state, j.type AS job_type,
                           j.started_at, j.finished_at, j.created_at AS job_created_at,
                           {produced} AS episodes_produced, {valid} AS episodes_valid
                    FROM job_contracts c JOIN jobs j ON j.id = c.job_id
                    WHERE c.outcome = 'pending'
                    ORDER BY c.created_at LIMIT %s""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_contract_outcome(
        self, job_id: str, outcome: str, observed: dict[str, Any]
    ) -> dict[str, Any] | None:
        with connect(self.settings) as connection:
            row = connection.execute(
                """UPDATE job_contracts
                      SET outcome = %s, observed = %s, updated_at = now()
                    WHERE job_id = %s RETURNING *""",
                (outcome, Jsonb(observed), job_id),
            ).fetchone()
        return dict(row) if row else None


# Public operations are wrapped once here so every current and future repository
# method reports `catalog_query_duration_seconds` without per-method decorator
# noise. Underscore helpers are internal and stay unwrapped (ADR 0017).
for _name, _method in list(vars(PostgresCatalog).items()):
    if _name.startswith("_") or not callable(_method):
        continue
    setattr(PostgresCatalog, _name, _timed_operation(_method))
del _name, _method
