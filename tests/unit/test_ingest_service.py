from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data_engine.ingest.service import EpisodeIngestService
from data_engine.storage.artifacts import FileArtifactStore


class CatalogStub:
    def __init__(self) -> None:
        self.arguments: dict[str, Any] | None = None
        self.calls: list[dict[str, Any]] = []

    def register_episode(self, **kwargs: Any) -> dict[str, Any]:
        self.arguments = kwargs
        self.calls.append(kwargs)
        return {"id": f"episode-{len(self.calls)}"}

    def record_episode_quality(self, episode_id: str, quality: dict[str, Any]) -> dict[str, Any]:
        self.quality = quality
        return {"episode_id": episode_id, "verdict": quality["verdict"]}


@pytest.mark.unit
def test_synthetic_ingest_records_motion_quality(tmp_path: Path) -> None:
    catalog = CatalogStub()
    service = EpisodeIngestService(catalog, FileArtifactStore(tmp_path))  # type: ignore[arg-type]
    frames = 10
    episode = {
        "task": "pick",
        "robot": "arm",
        "timestamps": [i * 0.1 for i in range(frames)],
        "observations": [[float(i), float(i) * 2] for i in range(frames)],
        "actions": [[float(i), float(i) * 2] for i in range(frames)],
    }

    service.ingest(episode, job_id="job-1")

    quality = catalog.quality
    assert quality["frame_count"] == frames
    assert quality["verdict"] == "smooth"
    # obs[0], action[0] step 1.0 and obs[1], action[1] step 2.0 -> L2 sqrt(10).
    assert quality["movement_score"] == pytest.approx(10**0.5)
    assert quality["stall_ratio"] == 0.0
    assert {dim["name"] for dim in quality["dims"]} == {
        "observation[0]",
        "observation[1]",
        "action[0]",
        "action[1]",
    }


@pytest.mark.unit
def test_synthetic_episode_ingest_stores_artifact_and_metadata(tmp_path: Path) -> None:
    catalog = CatalogStub()
    artifacts = FileArtifactStore(tmp_path)
    service = EpisodeIngestService(catalog, artifacts)  # type: ignore[arg-type]
    episode = {
        "task": "pick",
        "robot": "arm",
        "timestamps": [0.0, 0.25],
        "observations": [[0.0], [1.0]],
        "actions": [[0.1], [0.2]],
    }

    result = service.ingest(episode, job_id="job-1")

    assert result["episode_id"] == "episode-1"
    assert result["artifact_hash"] == result["source_hash"]
    assert result["format"] == "synthetic-json"
    assert artifacts.get_bytes(result["artifact_hash"])
    assert catalog.arguments is not None
    assert catalog.arguments["job_id"] == "job-1"
    assert catalog.arguments["metadata"] == {
        "format": "synthetic-json",
        "task": "pick",
        "robot": "arm",
        "frame_count": 2,
        "duration_seconds": 0.25,
    }


def _lerobot_root(root: Path) -> Path:
    """A two-episode v3 dataset, packed into one Parquet file as the real format is."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "meta").mkdir(exist_ok=True)
    (root / "meta" / "info.json").write_text(
        '{"codebase_version": "v3.0", "robot_type": "so100_follower", "fps": 10,'
        ' "total_episodes": 2, "features": {"action": {"dtype": "float32"}}}',
        encoding="utf-8",
    )
    rows = pa.table(
        {
            "action": [[float(i)] for i in range(6)],
            "timestamp": [float(i) for i in range(6)],
            "frame_index": list(range(6)),
            "episode_index": [0] * 3 + [1] * 3,
            "index": list(range(6)),
            "task_index": [0] * 6,
        }
    )
    data_dir = root / "data" / "chunk-000"
    data_dir.mkdir(parents=True)
    pq.write_table(rows, data_dir / "file-000.parquet")
    index_dir = root / "meta" / "episodes" / "chunk-000"
    index_dir.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "episode_index": [0, 1],
                "data/chunk_index": [0, 0],
                "data/file_index": [0, 0],
                "dataset_from_index": [0, 3],
                "dataset_to_index": [3, 6],
                "tasks": [["first"], ["second"]],
                "length": [3, 3],
            }
        ),
        index_dir / "file-000.parquet",
    )
    return root


@pytest.mark.unit
def test_path_ingest_addresses_the_source_file_and_reports_the_episode(
    tmp_path: Path,
) -> None:
    """The artifact is the file the rows came from; identity is (file, episode)."""
    catalog = CatalogStub()
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    service = EpisodeIngestService(catalog, artifacts)  # type: ignore[arg-type]
    root = _lerobot_root(tmp_path / "ds")

    result = service.ingest_path(root, job_id="job-1", episode_key="episode_index=1")

    assert result["format"] == "lerobot-v3"
    assert result["episode_key"] == "episode_index=1"
    assert result["frame_count"] == 3
    source_file = root / "data" / "chunk-000" / "file-000.parquet"
    assert artifacts.get_bytes(result["artifact_hash"]) == source_file.read_bytes()
    metadata = catalog.arguments["metadata"]
    assert metadata["task"] == "second"
    assert metadata["dataset"]["total_episodes"] == 2


@pytest.mark.unit
def test_two_episodes_from_one_file_get_distinct_catalog_rows(tmp_path: Path) -> None:
    catalog = CatalogStub()
    service = EpisodeIngestService(catalog, FileArtifactStore(tmp_path))  # type: ignore[arg-type]
    root = _lerobot_root(tmp_path / "ds")

    first = service.ingest_path(root, job_id="job-1", episode_key="episode_index=0")
    second = service.ingest_path(root, job_id="job-2", episode_key="episode_index=1")

    assert first["artifact_hash"] == second["artifact_hash"], "one file, one content address"
    assert first["episode_id"] != second["episode_id"]
    assert catalog.calls[0]["episode_key"] != catalog.calls[1]["episode_key"]
