from pathlib import Path
from typing import Any

import pytest

from data_engine.config import Settings
from data_engine.jobs.state import JobState
from data_engine.jobs.worker import IngestWorker


class FakeCatalog:
    def __init__(self, job: dict[str, Any] | None) -> None:
        self.job = job
        self.state = "queued" if job else None
        self.episode: dict[str, Any] | None = None

    def claim_job(self) -> dict[str, Any] | None:
        if self.job is None:
            return None
        self.state = "running"
        return self.job

    def register_episode(self, **kwargs: Any) -> dict[str, Any]:
        self.episode = {
            "id": "episode-1",
            "artifact_hash": kwargs["artifact_hash"],
            "metadata": kwargs["metadata"],
        }
        return self.episode

    def finish_job(self, job_id: str, state: JobState, **kwargs: Any) -> dict[str, Any]:
        self.state = state.value
        if kwargs.get("error") is not None:
            self.error = kwargs["error"]
        return {"id": job_id, "state": state.value, **kwargs}

    def get_job(self, job_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {"id": job_id, "state": self.state}
        if hasattr(self, "error"):
            result["error"] = self.error
        return result


def _job(episode: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "job-1",
        "type": "ingest",
        "state": "running",
        "correlation_id": "corr-1",
        "payload": {
            "episode": episode
            or {
                "task": "pick",
                "robot": "test-arm",
                "timestamps": [0.0, 0.1],
                "observations": [[0.0], [1.0]],
                "actions": [[0.1], [0.2]],
            }
        },
    }


@pytest.mark.unit
def test_worker_processes_episode_and_stores_content_addressed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(_job())
    monkeypatch.setattr("data_engine.jobs.worker.PostgresCatalog", lambda _settings: catalog)
    worker = IngestWorker(Settings(artifact_root=tmp_path, _env_file=None))

    result = worker.process_one()

    assert result is not None
    assert result["state"] == "succeeded"
    assert result["result"]["episode_id"] == "episode-1"
    assert catalog.episode is not None
    assert worker.artifacts.get_bytes(result["result"]["artifact_hash"])


@pytest.mark.unit
def test_worker_returns_none_when_no_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = FakeCatalog(None)
    monkeypatch.setattr("data_engine.jobs.worker.PostgresCatalog", lambda _settings: catalog)
    worker = IngestWorker(Settings(artifact_root=tmp_path, _env_file=None))
    assert worker.process_one() is None


@pytest.mark.unit
def test_worker_marks_invalid_job_failed_without_leaking_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = FakeCatalog(_job({"task": "missing-fields"}))
    monkeypatch.setattr("data_engine.jobs.worker.PostgresCatalog", lambda _settings: catalog)
    worker = IngestWorker(Settings(artifact_root=tmp_path, _env_file=None))

    result = worker.process_one()

    assert result is not None
    assert result["state"] == "failed"
    assert result["error"]["message"] == "job handler failed"
