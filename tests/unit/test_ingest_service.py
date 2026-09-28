from pathlib import Path
from typing import Any

import pytest

from data_engine.ingest.service import SyntheticEpisodeIngestService
from data_engine.storage.artifacts import FileArtifactStore


class CatalogStub:
    def __init__(self) -> None:
        self.arguments: dict[str, Any] | None = None

    def register_episode(self, **kwargs: Any) -> dict[str, Any]:
        self.arguments = kwargs
        return {"id": "episode-1"}


@pytest.mark.unit
def test_synthetic_episode_ingest_stores_artifact_and_metadata(tmp_path: Path) -> None:
    catalog = CatalogStub()
    artifacts = FileArtifactStore(tmp_path)
    service = SyntheticEpisodeIngestService(catalog, artifacts)  # type: ignore[arg-type]
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
    assert artifacts.get_bytes(result["artifact_hash"])
    assert catalog.arguments is not None
    assert catalog.arguments["job_id"] == "job-1"
    assert catalog.arguments["metadata"] == {
        "task": "pick",
        "robot": "arm",
        "frame_count": 2,
        "duration_seconds": 0.25,
    }
