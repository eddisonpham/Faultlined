"""Vertical-slice worker: claim an ingest job, store synthetic episode, record lineage."""

from __future__ import annotations

import errno
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from data_engine.builds import BuildError, DatasetBuilder
from data_engine.builds.export import BuildExporter, ExportBuildUnknown, ExportError
from data_engine.catalog.repository import IdempotencyConflict, PostgresCatalog
from data_engine.config import Settings
from data_engine.ingest.readers.base import ReaderError
from data_engine.ingest.service import EpisodeIngestService
from data_engine.jobs.state import JobState, JobType
from data_engine.observability.logging import correlation_id_var
from data_engine.observability.metrics import RuntimeMetrics
from data_engine.observability.reason_codes import ReasonCode
from data_engine.storage.artifacts import FileArtifactStore
from data_engine.validation.profile import InvalidProfile, profile_from_dict, profile_hash
from data_engine.validation.service import ValidationService

logger = logging.getLogger(__name__)


class UnsupportedJobType(ValueError):
    """A claimed job is not handled by this worker."""


class InvalidJobPayload(ValueError):
    """A claimed job's payload does not match the handler contract."""


class _JobTimedOut(Exception):
    """Internal signal that a job exceeded its deadline before or during execution."""


class _CancelRequested(Exception):
    """Raised by the handler when the worker reports cancellation."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"cancel requested for job {job_id}")
        self.job_id = job_id


_TERMINAL_FAILURES: tuple[type[Exception], ...] = (
    ReaderError,
    InvalidJobPayload,
    UnsupportedJobType,
    IdempotencyConflict,
    InvalidProfile,
    ExportBuildUnknown,
    ExportError,
    psycopg.errors.DataError,
)


def _is_terminal(exc: Exception) -> bool:
    return isinstance(exc, _TERMINAL_FAILURES)


def _failure_reason_code(exc: Exception) -> ReasonCode:
    if isinstance(exc, ExportBuildUnknown):
        return ReasonCode.EXPORT_BUILD_UNKNOWN
    if isinstance(exc, ExportError):
        return ReasonCode.EXPORT_FAILED
    if isinstance(exc, InvalidJobPayload):
        return ReasonCode.INGEST_PARSE_FAILED
    if isinstance(exc, ReaderError):
        return ReasonCode.INGEST_PARSE_FAILED
    if isinstance(exc, UnsupportedJobType):
        return ReasonCode.INGEST_FORMAT_UNKNOWN
    if isinstance(exc, InvalidProfile):
        return ReasonCode.VALIDATION_PROFILE_INVALID
    if isinstance(exc, psycopg.errors.DataError):
        return ReasonCode.CATALOG_WRITE_REJECTED
    if isinstance(exc, FileNotFoundError):
        return ReasonCode.IO_ARTIFACT_MISSING
    if isinstance(exc, OSError):
        return ReasonCode.IO_DISK_FULL if exc.errno == errno.ENOSPC else ReasonCode.IO_WRITE_FAILED
    if isinstance(exc, KeyError):
        return ReasonCode.INGEST_PARSE_FAILED
    return ReasonCode.INTERNAL_ERROR


_MAX_ERROR_MESSAGE = 400
_URL_CREDENTIALS = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*=\s*\S+")


def _error_message(exc: Exception) -> str:
    """One line of the exception's own text, capped and scrubbed of credentials."""
    text = " ".join(str(exc).split())
    text = _URL_CREDENTIALS.sub("://***:***@", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=***", text)
    if len(text) > _MAX_ERROR_MESSAGE:
        text = text[: _MAX_ERROR_MESSAGE - 1].rstrip() + "\u2026"
    return text or "job handler failed"


def _failure_payload(exc: Exception, reason_code: ReasonCode) -> dict[str, Any]:
    """What is persisted on the job row when a handler fails, and rendered by the job report and
    `/ui/jobs/{id}`: the exception type, its own sanitized message, and the stable reason code
    the API contract already promises.
    """
    return {
        "type": type(exc).__name__,
        "message": _error_message(exc),
        "reason_code": reason_code.value,
    }


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
        catalog: PostgresCatalog | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.catalog = catalog or PostgresCatalog(self.settings, metrics=metrics)
        self.artifacts = FileArtifactStore(self.settings.artifact_root)
        self.ingest = EpisodeIngestService(self.catalog, self.artifacts, metrics=metrics)
        self.validation = ValidationService(self.catalog, metrics=metrics)
        self._cancel_requested = False
        self.metrics = metrics or RuntimeMetrics()

    def _run_handler(self, job: dict[str, Any], job_type: str) -> dict[str, Any]:
        """Dispatch one claimed job to its handler."""
        payload = job["payload"]
        if job_type == JobType.INGEST_SOURCE:
            source = payload.get("source")
            if not isinstance(source, str) or not source:
                raise InvalidJobPayload("payload.source must be a non-empty path")
            episode_key = payload.get("episode_key")
            if episode_key is not None and not isinstance(episode_key, str):
                raise InvalidJobPayload("payload.episode_key must be a string when present")
            return self.ingest.ingest_path(
                Path(source), job_id=str(job["id"]), episode_key=episode_key
            )
        if job_type == JobType.INGEST:
            episode = payload.get("episode")
            if not isinstance(episode, dict):
                raise InvalidJobPayload("payload.episode must be an object")
            return self.ingest.ingest(episode, job_id=str(job["id"]))
        if job_type == JobType.VALIDATE:
            return self._validate(payload)
        if job_type == JobType.BUILD:
            return self._build(payload, job_id=str(job["id"]))
        if job_type == JobType.EXPORT:
            return self._export(payload, job_id=str(job["id"]))
        raise UnsupportedJobType(f"unsupported job type: {job_type}")

    def _selection(
        self, payload: dict[str, Any], *, field: str, state: str | None = None
    ) -> list[str]:
        """The episodes a job names, or every episode in `state` if it names none."""
        singular = payload.get("episode_id")
        if isinstance(singular, str) and singular:
            return [singular]
        raw = payload.get(field, [])
        if raw is None:
            raw = []
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise InvalidJobPayload(f"payload.{field} must be a list of episode ids")
        if raw:
            return [str(item) for item in raw]
        return [str(row["id"]) for row in self.catalog.list_episodes(limit=10_000, state=state)]

    def _validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Record validation results for episodes already in the catalog (FR-002)."""
        profile = payload.get("profile")
        if not isinstance(profile, dict) or not profile:
            raise InvalidJobPayload("payload.profile must be a profile document")
        document = profile_from_dict(profile)
        self.catalog.register_validation_profile(document)

        selected = self._selection(payload, field="episode_ids")
        outcomes: list[dict[str, Any]] = []
        for episode_id in selected:
            result = self.validation.validate_stored_episode(episode_id, document)
            outcomes.append(
                {
                    "episode_id": episode_id,
                    "profile_hash": result.profile_hash,
                    "profile_name": result.profile_name,
                    "passed": result.passed,
                    "reason_codes": list(result.reason_codes),
                }
            )
        passed = [row for row in outcomes if row["passed"]]
        if not outcomes:
            raise InvalidJobPayload("no episodes to validate: the catalog is empty")
        return {
            "profile_hash": profile_hash(document),
            "profile_name": document.name,
            "checked": len(outcomes),
            "passed": len(passed),
            "failed": len(outcomes) - len(passed),
            "episodes": outcomes,
        }

    def _build(self, payload: dict[str, Any], *, job_id: str) -> dict[str, Any]:
        """Assemble a deterministic dataset build over a selection (FR-006/007)."""
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidJobPayload("payload.name must be a non-empty build name")

        selected = self._selection(payload, field="episode_ids", state="valid")
        episodes = self.catalog.get_episodes(selected)
        found = {str(row["id"]) for row in episodes}
        missing = [episode_id for episode_id in selected if episode_id not in found]
        if missing:
            raise InvalidJobPayload(f"unknown episode id(s): {', '.join(sorted(missing)[:5])}")

        profile_document = None
        raw_profile = payload.get("profile")
        if isinstance(raw_profile, dict) and raw_profile:
            profile_document = profile_from_dict(raw_profile)
            self.catalog.register_validation_profile(profile_document)

        builder = DatasetBuilder(code_commit=self._code_commit())
        try:
            build = builder.build(
                episodes,
                name=name.strip(),
                profile=profile_document.to_dict() if profile_document else None,
            )
        except BuildError as exc:
            raise InvalidJobPayload(str(exc)) from exc

        self.catalog.record_build(build, job_id=job_id)
        return {
            "build_hash": build.hash,
            "name": build.name,
            "episode_count": build.episode_count,
            "profile_hash": build.profile_hash,
            "code_commit": build.code_commit,
        }

    def _export(self, payload: dict[str, Any], *, job_id: str) -> dict[str, Any]:
        """Materialise a build as a LeRobot v3 dataset on disk (ADR 0025)."""
        build_hash = payload.get("build_hash")
        if not isinstance(build_hash, str) or not build_hash.strip():
            raise InvalidJobPayload("payload.build_hash must be a non-empty build hash")
        build_hash = build_hash.strip()
        build = self.catalog.get_build(build_hash)
        if build is None:
            raise ExportBuildUnknown(f"unknown build hash: {build_hash}")
        members = self.catalog.build_episodes(build_hash)
        if not members:
            raise InvalidJobPayload(f"build {build_hash} has no member episodes")
        member_rows = self.catalog.get_episodes([str(m["episode_id"]) for m in members])
        found = {str(row["id"]) for row in member_rows}
        missing = [str(m["episode_id"]) for m in members if str(m["episode_id"]) not in found]
        if missing:
            raise InvalidJobPayload(
                f"build members no longer in the catalog: {', '.join(missing[:5])}"
            )
        exporter = BuildExporter(self.settings.export_root, self.artifacts)
        return {**exporter.export(build, member_rows), "job_id": job_id}

    def _code_commit(self) -> str:
        """The commit that produced a build, when the checkout can name one."""
        try:
            head = Path(__file__).resolve().parents[3] / ".git" / "HEAD"
            if head.exists():
                ref = head.read_text(encoding="utf-8").strip()
                if ref.startswith("ref:"):
                    pointer = head.parent / ref.split(" ", 1)[1]
                    if pointer.exists():
                        return pointer.read_text(encoding="utf-8").strip()[:12]
                    return ref.split(" ", 1)[1].rsplit("/", 1)[-1][:12]
                return ref[:12]
        except OSError:
            pass
        return ""

    def _respect_deadline(self, job: dict[str, Any]) -> None:
        """Time the job out before running if its deadline already passed."""
        deadline = job.get("deadline_at")
        if isinstance(deadline, datetime) and deadline < datetime.now(UTC):
            self.catalog.mark_deadline_expired(str(job["id"]))
            self.metrics.job_timeout(job_type=str(job["type"]))
            raise _JobTimedOut(str(job["id"]))

    def _settle_failure(
        self,
        job: dict[str, Any],
        error: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> dict[str, Any] | None:
        """Decide between retrying and failing, honouring attempts and cancellation."""
        job_id = str(job["id"])
        job_type = str(job["type"])
        if self.catalog.is_cancel_requested(job_id):
            cancelled = self.catalog.finish_job(job_id, JobState.CANCELED, error=error)
            self.metrics.job_cancel(job_type=job_type)
            return cancelled

        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or 1)
        if not terminal and attempts < max_attempts:
            self.catalog.finish_job(job_id, JobState.RETRYING, error=error)
            self.catalog.requeue_for_retry(job_id)
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
                "terminal": terminal,
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
            self.metrics.worker_heartbeat(worker_state="idle")
            return None
        self.metrics.worker_heartbeat(worker_state="busy")
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
            self._checkpoint(job, started)
            result = self._run_handler(job, job_type)
            self._respect_running_deadline(job)
            episode_id = str(result.get("episode_id") or "")
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
        except _CancelRequested:
            self._settle_failure(
                job,
                {
                    "type": "CancelRequested",
                    "message": "cancel requested while job was running",
                    "reason_code": ReasonCode.JOB_CANCELED.value,
                },
                terminal=False,
            )
            return self.catalog.get_job(str(job["id"]))
        except Exception as exc:
            stage_seconds = time.perf_counter() - started
            reason_code = _failure_reason_code(exc)
            self.metrics.job_run_time(stage_seconds, job_type=job_type, state=JobState.FAILED.value)
            self.metrics.stage_duration(
                seconds=stage_seconds, stage="ingest", status=JobState.FAILED.value
            )
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
            self._settle_failure(
                job,
                _failure_payload(exc, reason_code),
                terminal=_is_terminal(exc),
            )
            return self.catalog.get_job(str(job["id"]))
        finally:
            correlation_id_var.reset(token)

    @property
    def cancel_requested(self) -> bool:
        """Cancellation status: local flag first, then the catalog."""
        return self._cancel_requested or self.catalog.is_cancel_requested(
            str(getattr(self.catalog, "id", "")) or ""
        )

    def _checkpoint(self, job: dict[str, Any], _started: float) -> None:
        """Cancel-check + the only interruption point the worker honors."""
        if self.cancel_requested:
            raise _CancelRequested(str(job["id"]))

    def _respect_running_deadline(self, job: dict[str, Any]) -> None:
        """Time an in-flight job out if its wall-clock budget expired mid-run."""
        deadline = job.get("deadline_at")
        if isinstance(deadline, datetime) and deadline < datetime.now(UTC):
            self.catalog.mark_deadline_expired(str(job["id"]))
            self.metrics.job_timeout(job_type=str(job["type"]))
            raise _JobTimedOut(str(job["id"]))
