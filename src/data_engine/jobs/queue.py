"""Job queue facade over the Postgres-backed catalog."""

from __future__ import annotations

from typing import Any

from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings


class PostgresJobQueue:
    def __init__(self, settings: Settings | None = None) -> None:
        self.catalog = PostgresCatalog(settings)

    def submit(
        self,
        job_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        correlation_id: str,
    ) -> tuple[dict[str, Any], bool]:
        return self.catalog.submit_job(job_type, payload, idempotency_key, correlation_id)

    def claim(self) -> dict[str, Any] | None:
        return self.catalog.claim_job()
