"""Episode ingestion operations used by worker handlers."""

from __future__ import annotations

from typing import Any

from data_engine.catalog.repository import PostgresCatalog, canonical_json
from data_engine.storage.artifacts import FileArtifactStore


class SyntheticEpisodeIngestService:
    """Canonicalize and register the small synthetic JSON episode contract."""

    def __init__(self, catalog: PostgresCatalog, artifacts: FileArtifactStore) -> None:
        self.catalog = catalog
        self.artifacts = artifacts

    def ingest(self, episode: dict[str, Any], *, job_id: str) -> dict[str, Any]:
        encoded = canonical_json(episode)
        artifact_hash = self.artifacts.put_bytes(encoded)
        timestamps = episode["timestamps"]
        metadata = {
            "task": episode["task"],
            "robot": episode["robot"],
            "frame_count": len(timestamps),
            "duration_seconds": timestamps[-1] - timestamps[0],
        }
        episode_row = self.catalog.register_episode(
            source_hash=artifact_hash,
            artifact_hash=artifact_hash,
            size_bytes=len(encoded),
            metadata=metadata,
            job_id=job_id,
        )
        return {
            "episode_id": episode_row["id"],
            "source_hash": artifact_hash,
            "artifact_hash": artifact_hash,
        }
