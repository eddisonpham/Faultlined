"""Episode ingestion operations used by worker handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from data_engine.catalog.repository import PostgresCatalog, canonical_json
from data_engine.ingest.readers.base import EpisodeExtraction, ReaderError
from data_engine.ingest.readers.registry import read_episode
from data_engine.observability.metrics import RuntimeMetrics
from data_engine.storage.artifacts import FileArtifactStore

# The synthetic contract is the smallest thing this engine reads, and it is read the
# same way as a file: validate the declared shape, then describe the episode. Letting
# a malformed payload raise a bare `KeyError` would leak an implementation detail into
# the job error and, worse, would make the failure look transient to the retry policy.
_SYNTHETIC_FIELDS = ("task", "robot", "timestamps", "observations", "actions")


def _frames(episode: dict[str, Any]) -> list[float]:
    missing = [name for name in _SYNTHETIC_FIELDS if name not in episode]
    if missing:
        raise ReaderError(f"synthetic episode is missing {', '.join(missing)}")
    timestamps = episode["timestamps"]
    if not isinstance(timestamps, list) or not timestamps:
        raise ReaderError("synthetic episode timestamps must be a non-empty list")
    if len(episode["observations"]) != len(timestamps) or len(episode["actions"]) != len(
        timestamps
    ):
        raise ReaderError("timestamps, observations, and actions must have equal lengths")
    return [float(value) for value in timestamps]


class EpisodeIngestService:
    """Registers one episode in the catalog, whatever format it arrived in.

    Two paths, one contract. The synthetic path takes an episode that is already in
    memory; the reader path takes a path on disk and returns a *description* of one
    episode. Both end the same way: the bytes are content-addressed in the artifact
    store, and a small metadata record is what the catalog keeps
    (`architecture/storage.md` §1). The frames themselves are never copied into the
    catalog, which is what keeps ingest O(one episode) instead of O(dataset).
    """

    def __init__(
        self,
        catalog: PostgresCatalog,
        artifacts: FileArtifactStore,
        *,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.catalog = catalog
        self.artifacts = artifacts
        self.metrics = metrics or RuntimeMetrics()

    def ingest(self, episode: dict[str, Any], *, job_id: str) -> dict[str, Any]:
        """Register an in-memory episode (the synthetic JSON contract)."""
        timestamps = _frames(episode)
        encoded = canonical_json(episode)
        artifact_hash = self.artifacts.put_bytes(encoded)
        metadata = {
            "format": "synthetic-json",
            "task": episode["task"],
            "robot": episode["robot"],
            "frame_count": len(timestamps),
            "duration_seconds": timestamps[-1] - timestamps[0],
        }
        return self._register(
            source_hash=artifact_hash,
            artifact_hash=artifact_hash,
            size_bytes=len(encoded),
            metadata=metadata,
            job_id=job_id,
            episode_format="synthetic-json",
        )

    def ingest_path(
        self, path: Path, *, job_id: str, episode_key: str | None = None
    ) -> dict[str, Any]:
        """Register one episode read from disk, addressing the file that holds it.

        The artifact is the reader's `source_path` - the Parquet file the rows came
        from - not the dataset root. A LeRobot v3 file holds many episodes, so the
        artifact is shared between them and the episode identity lives in the catalog
        row; that is exactly the content-addressing model, and it is why the reader
        reports the file it actually read.
        """
        extraction = read_episode(path, episode_key=episode_key)
        return self._register_extraction(extraction, job_id=job_id)

    def _register_extraction(self, extraction: EpisodeExtraction, *, job_id: str) -> dict[str, Any]:
        source = extraction.source_path
        digest = self.artifacts.put_file(source)
        return self._register(
            source_hash=digest,
            artifact_hash=digest,
            size_bytes=source.stat().st_size,
            metadata=extraction.metadata() | {"dataset": extraction.dataset},
            job_id=job_id,
            episode_format=extraction.format,
            episode_key=extraction.episode_key,
        )

    def _register(
        self,
        *,
        source_hash: str,
        artifact_hash: str,
        size_bytes: int,
        metadata: dict[str, Any],
        job_id: str,
        episode_format: str,
        episode_key: str | None = None,
    ) -> dict[str, Any]:
        episode_row = self.catalog.register_episode(
            source_hash=source_hash,
            artifact_hash=artifact_hash,
            size_bytes=size_bytes,
            metadata=metadata,
            job_id=job_id,
            episode_key=episode_key,
            episode_format=episode_format,
        )
        self.metrics.artifact_written(size_bytes)
        self.metrics.episode_ingested(episode_format=episode_format, status="succeeded")
        return {
            "episode_id": episode_row["id"],
            "source_hash": source_hash,
            "artifact_hash": artifact_hash,
            "format": episode_format,
            "episode_key": episode_key,
            "frame_count": metadata.get("frame_count"),
        }


# The name the worker and its tests already use. Kept as an alias because renaming it
# would touch every existing test for no behavioural gain.
SyntheticEpisodeIngestService = EpisodeIngestService
