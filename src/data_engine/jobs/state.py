"""Job types and lifecycle states (state machine: architecture/data-flow.md §4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

DEFAULT_MAX_ATTEMPTS = 3

ORPHANED_JOB_SECONDS = 900.0

REAP_INTERVAL_SECONDS = 30.0


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMED_OUT = "timed_out"


TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELED, JobState.TIMED_OUT}
)

ACTIVE_STATES: frozenset[JobState] = frozenset(
    {JobState.QUEUED, JobState.RUNNING, JobState.RETRYING, JobState.CANCEL_REQUESTED}
)

ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELED, JobState.TIMED_OUT}),
    JobState.RUNNING: frozenset(
        {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.RETRYING,
            JobState.TIMED_OUT,
            JobState.CANCEL_REQUESTED,
        }
    ),
    JobState.CANCEL_REQUESTED: frozenset({JobState.CANCELED, JobState.SUCCEEDED}),
    JobState.RETRYING: frozenset({JobState.QUEUED, JobState.FAILED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELED: frozenset(),
    JobState.TIMED_OUT: frozenset(),
}


class JobType(StrEnum):
    INGEST = "ingest"
    INGEST_SOURCE = "ingest_source"
    VALIDATE = "validate"
    INDEX = "index"
    BUILD = "build"
    EXPORT = "export"
    WORKLOAD = "workload"
    GC = "gc"


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    type: JobType
    state: JobState
    priority: int = 0
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    idempotency_key: str | None = None
    parent_job_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    deadline: datetime | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
