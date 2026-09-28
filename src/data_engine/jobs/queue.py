"""Queue interface; Postgres-backed implementation follows in phase 05."""

from typing import Protocol

from data_engine.jobs.state import Job, JobType


class JobQueue(Protocol):
    """Submission and claim contract for job workers."""

    def submit(
        self, job_type: JobType, payload: dict[str, object], idempotency_key: str | None = None
    ) -> Job: ...

    def claim(self, worker_id: str) -> Job | None: ...
