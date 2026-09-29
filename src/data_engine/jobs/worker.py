"""Vertical-slice worker: claim an ingest job, store synthetic episode, record lineage."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from data_engine.ingest.service import SyntheticEpisodeIngestService
from data_engine.jobs.state import JobState
from data_engine.observability.logging import correlation_id_var
from data_engine.observability.metrics import RuntimeMetrics
from data_engine.observability.reason_codes import ReasonCode
from data_engine.storage.artifacts import FileArtifactStore

logger = logging.getLogger(__name__)


class UnsupportedJobType(ValueError):
    """A claimed job is not handled by this worker."""


class InvalidJobPayload(ValueError):
    """A claimed job's payload does not match the handler contract."""


class _JobTimedOut(Exception):
    """Internal signal that a job exceeded its deadline before or during execution."""


def _failure_reason_code(exc: Exception) -> ReasonCode:
    if isinstance(exc, InvalidJobPayload):
        return ReasonCode.INGEST_PARSE_FAILED
    if isinstance(exc, UnsupportedJobType):
        return ReasonCode.INGEST_FORMAT_UNKNOWN
    if isinstance(exc, KeyError):
        return ReasonCode.INGEST_PARSE_FAILED
    return ReasonCode.INTERNAL_ERROR


def _elapsed_seconds(earlier: object, later: object) -> float | None:
    """Seconds between two datetimes, or None when either is missing/unparsable."""
    if not isinstance(earlier, datetime) or not isinstance(later, datetime):
        return None
    delta = (later - earlier).total_seconds()
    return delta if delta >= 0 else None


class IngestWorker:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.catalog = PostgresCatalog(self.settings)
        self.artifacts = FileArtifactStore(self.settings.artifact_root)
        self.ingest = SyntheticEpisodeIngestService(self.catalog, self.artifacts, metrics=metrics)
        self.metrics = metrics or RuntimeMetrics()

    def _respect_deadline(self, job: dict[str, Any]) -> None:
        """Time the job out before running if its deadline already passed."""
        deadline = job.get("deadline_at")
        if isinstance(deadline, datetime) and deadline < datetime.now(UTC):
            self.catalog.mark_deadline_expired(str(job["id"]))
            self.metrics.job_timeout(job_type=str(job["type"]))
            raise _JobTimedOut(str(job["id"]))

    def _settle_failure(self, job: dict[str, Any], error: dict[str, Any]) -> dict[str, Any] | None:
        """Decide between retrying and failing, honouring attempts and cancellation.

        Returns the updated job row, or None when the job went back to the queue and a
        later attempt will pick it up.
        """
        job_id = str(job["id"])
        job_type = str(job["type"])
        if self.catalog.is_cancel_requested(job_id):
            cancelled = self.catalog.finish_job(job_id, JobState.CANCELED, error=error)
            self.metrics.job_cancel(job_type=job_type)
            return cancelled

        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or 1)
        if attempts < max_attempts:
            self.catalog.finish_job(job_id, JobState.RETRYING, error=error)
            self.catalog.requeue_for_retry(job_id)
            # `attempts` is 1-based here: claim_job already incremented it, so this is
            # the attempt that just failed, not the one that will run next.
            self.metrics.job_retry(job_type=job_type, attempt=attempts)
            logger.warning(
                "job scheduled for retry",
                extra={
                    "event": "job_retry_scheduled",
                    "job_id": job_id,
                    "attempt": attempts,
                    "next_attempt": attempts + 1,
                    "max_attempts": max_attempts,
                    "correlation_id": str(job["correlation_id"]),
                },
            )
            return None

        failed = self.catalog.finish_job(job_id, JobState.FAILED, error=error)
        logger.error(
            "job failed permanently",
            extra={
                "event": "job_failed_permanently",
                "job_id": job_id,
                "attempts": attempts,
                "correlation_id": str(job["correlation_id"]),
            },
        )
        return failed

    def _record_queue_signals(self, job: dict[str, Any]) -> None:
        """Queue depth after the claim, plus time spent waiting for this job."""
        self.metrics.queue_depth(self.catalog.count_jobs(JobState.QUEUED))
        job_type = str(job["type"])
        waited = _elapsed_seconds(job.get("created_at"), job.get("started_at"))
        if waited is not None:
            self.metrics.job_queue_time(waited, job_type=job_type)

    def process_one(self) -> dict[str, Any] | None:
        job = self.catalog.claim_job()
        if job is None:
            self.metrics.queue_depth(self.catalog.count_jobs(JobState.QUEUED))
            return None
        correlation_id = str(job["correlation_id"])
        token = correlation_id_var.set(correlation_id)
        logger.info(
            "claimed job",
            extra={"event": "job_claimed", "job_id": job["id"], "correlation_id": correlation_id},
        )
        self._record_queue_signals(job)
        job_type = str(job["type"])
        started = time.perf_counter()
        try:
            self._respect_deadline(job)
            if job["type"] != "ingest":
                raise UnsupportedJobType(f"unsupported job type: {job['type']}")
            payload = job["payload"]
            episode = payload.get("episode")
            if not isinstance(episode, dict):
                raise InvalidJobPayload("payload.episode must be an object")
            result = self.ingest.ingest(episode, job_id=str(job["id"]))
            episode_id = str(result["episode_id"])
            completed = self.catalog.finish_job(str(job["id"]), JobState.SUCCEEDED, result=result)
            stage_seconds = time.perf_counter() - started
            self.metrics.job_run_time(
                stage_seconds, job_type=job_type, state=JobState.SUCCEEDED.value
            )
            self.metrics.stage_duration(
                stage_seconds, stage="ingest", status=JobState.SUCCEEDED.value
            )
            logger.info(
                "ingest job succeeded",
                extra={
                    "event": "job_succeeded",
                    "job_id": job["id"],
                    "episode_id": episode_id,
                    "correlation_id": correlation_id,
                },
            )
            return completed
        except _JobTimedOut:
            return self.catalog.get_job(str(job["id"]))
        except Exception as exc:
            stage_seconds = time.perf_counter() - started
            reason_code = _failure_reason_code(exc)
            self.metrics.job_run_time(stage_seconds, job_type=job_type, state=JobState.FAILED.value)
            self.metrics.stage_duration(stage_seconds, stage="ingest", status=JobState.FAILED.value)
            self.metrics.job_failure(job_type=job_type, reason_code=reason_code.value)
            logger.error(
                "ingest job failed",
                extra={
                    "event": "job_failed",
                    "job_id": job["id"],
                    "correlation_id": correlation_id,
                    "error_type": type(exc).__name__,
                    "reason_code": reason_code.value,
                },
            )
            self._settle_failure(job, {"type": type(exc).__name__, "message": "job handler failed"})
            return self.catalog.get_job(str(job["id"]))
        finally:
            correlation_id_var.reset(token)
