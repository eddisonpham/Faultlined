"""Vertical-slice worker: claim an ingest job, store synthetic episode, record lineage."""

from __future__ import annotations

import logging
from typing import Any

from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from data_engine.ingest.service import SyntheticEpisodeIngestService
from data_engine.jobs.state import JobState
from data_engine.observability.logging import correlation_id_var
from data_engine.storage.artifacts import FileArtifactStore

logger = logging.getLogger(__name__)


class IngestWorker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.catalog = PostgresCatalog(self.settings)
        self.artifacts = FileArtifactStore(self.settings.artifact_root)
        self.ingest = SyntheticEpisodeIngestService(self.catalog, self.artifacts)

    def process_one(self) -> dict[str, Any] | None:
        job = self.catalog.claim_job()
        if job is None:
            return None
        correlation_id = str(job["correlation_id"])
        token = correlation_id_var.set(correlation_id)
        logger.info(
            "claimed job",
            extra={"event": "job_claimed", "job_id": job["id"], "correlation_id": correlation_id},
        )
        try:
            if job["type"] != "ingest":
                raise ValueError(f"unsupported job type: {job['type']}")
            payload = job["payload"]
            episode = payload.get("episode")
            if not isinstance(episode, dict):
                raise ValueError("payload.episode must be an object")
            result = self.ingest.ingest(episode, job_id=str(job["id"]))
            episode_id = str(result["episode_id"])
            completed = self.catalog.finish_job(str(job["id"]), JobState.SUCCEEDED, result=result)
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
        except Exception as exc:
            logger.error(
                "ingest job failed",
                extra={
                    "event": "job_failed",
                    "job_id": job["id"],
                    "correlation_id": correlation_id,
                    "error_type": type(exc).__name__,
                },
            )
            self.catalog.finish_job(
                str(job["id"]),
                JobState.FAILED,
                error={"type": type(exc).__name__, "message": "job handler failed"},
            )
            return self.catalog.get_job(str(job["id"]))
        finally:
            correlation_id_var.reset(token)
