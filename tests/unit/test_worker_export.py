"""Worker-level tests for the `export` job type (ADR 0025).

The worker's export path is a selection-shaped handler like `build`: it names
resources that already exist (a build, its members) rather than carrying
bytes. The failure taxonomy is the point here - unknown build and malformed
build fail terminally, while disk errors retry - so the fake catalog exists to
make those distinctions observable without PostgreSQL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from data_engine.builds.export import FAULTLINED_DIR, ExportBuildUnknown
from data_engine.config import Settings
from data_engine.jobs.state import JobState
from data_engine.jobs.worker import IngestWorker
from tests.unit.test_worker import FakeCatalog


class ExportCatalog(FakeCatalog):
    """FakeCatalog plus the build-membership surface `_export` calls."""

    def __init__(self, job: dict[str, Any] | None) -> None:
        super().__init__(job)
        self.build: dict[str, Any] | None = None
        self.members: list[dict[str, Any]] = []
        self.episode_rows: list[dict[str, Any]] = []

    def get_build(self, build_id: str) -> dict[str, Any] | None:
        return self.build

    def build_episodes(self, build_id: str) -> list[dict[str, Any]]:
        return self.members

    def get_episodes(self, episode_ids: list[str]) -> list[dict[str, Any]]:
        wanted = set(episode_ids)
        return [row for row in self.episode_rows if str(row["id"]) in wanted]


def _export_job(build_hash: str, *, attempts: int = 1, max_attempts: int = 3) -> dict[str, Any]:
    from datetime import UTC, datetime

    created = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
    return {
        "id": "job-exp-1",
        "type": "export",
        "state": "running",
        "correlation_id": "corr-exp-1",
        "attempts": attempts,
        "max_attempts": max_attempts,
        "created_at": created,
        "started_at": created,
        "payload": {"build_hash": build_hash},
        "deadline_at": None,
    }


def _seeded_catalog(
    *,
    build_hash: str = "bld_x",
    with_episodes: bool = True,
) -> tuple[ExportCatalog, dict[str, Any]]:
    from data_engine.canonical import canonical_json

    blob = canonical_json(
        {
            "task": "pick",
            "robot": "so101",
            "timestamps": [0.0, 0.1],
            "observations": [[0.0], [0.1]],
            "actions": [[1.0], [1.1]],
        }
    )
    catalog = ExportCatalog(_export_job(build_hash))
    catalog.build = {
        "hash": build_hash,
        "name": "pick-set",
        "episode_count": 1,
        "manifest": {
            "kind": "dataset-build",
            "version": 1,
            "name": "pick-set",
            "code_commit": "abc123",
            "episode_count": 1,
            "episodes": [
                {
                    "episode_id": "episode-1",
                    "source_hash": "s" * 64,
                    "artifact_hash": "h-1",
                    "format": "synthetic-json",
                }
            ],
        },
    }
    catalog.members = [
        {"episode_id": "episode-1", "source_hash": "s" * 64, "artifact_hash": "h-1", "ordinal": 0}
    ]
    catalog.episode_rows = (
        [
            {
                "id": "episode-1",
                "artifact_hash": "h-1",
                "source_hash": "s" * 64,
                "episode_key": None,
                "metadata": {"robot": "so101", "task": "pick", "frame_count": 2},
            }
        ]
        if with_episodes
        else []
    )
    return catalog, {"h-1": blob}


def _worker(
    tmp_path: Path,
    catalog: ExportCatalog,
    blobs: dict[str, bytes],
    *,
    monkeypatch: pytest.MonkeyPatch,
    fail_reads: bool = False,
) -> IngestWorker:
    monkeypatch.setattr(
        "data_engine.jobs.worker.PostgresCatalog", lambda _settings, **_kwargs: catalog
    )
    worker = IngestWorker(
        Settings(artifact_root=tmp_path, export_root=tmp_path / "exports", _env_file=None)
    )
    if fail_reads:

        class Failing:
            def get_bytes(self, content_hash: str) -> bytes:
                raise OSError("disk went away")

        worker.artifacts = Failing()  # type: ignore[assignment]
    else:

        def get_bytes(content_hash: str) -> bytes:
            return blobs[content_hash]

        worker.artifacts.get_bytes = get_bytes  # type: ignore[method-assign]
    return worker


def test_export_job_succeeds_and_writes_the_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, blobs = _seeded_catalog()
    worker = _worker(tmp_path, catalog, blobs, monkeypatch=monkeypatch)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.SUCCEEDED.value
    payload = result["result"]
    assert payload["build_hash"] == "bld_x"
    assert payload["episode_count"] == 1
    tree = tmp_path / "exports" / "bld_x"
    assert (tree / "meta" / "info.json").is_file()
    assert (tree / FAULTLINED_DIR / "manifest.json").is_file()


def test_export_of_a_build_that_already_exists_is_a_successful_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, blobs = _seeded_catalog()
    worker = _worker(tmp_path, catalog, blobs, monkeypatch=monkeypatch)
    worker.process_one()

    # A second export with no readable artifacts at all still succeeds: the
    # verified tree at the content address is the answer.
    worker2 = _worker(tmp_path, catalog, {}, monkeypatch=monkeypatch, fail_reads=True)
    result = worker2.process_one()

    assert result is not None
    assert result["state"] == JobState.SUCCEEDED.value
    assert result["result"]["path"] == str(tmp_path / "exports" / "bld_x")


def test_unknown_build_hash_fails_terminally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, blobs = _seeded_catalog()
    catalog.build = None
    worker = _worker(tmp_path, catalog, blobs, monkeypatch=monkeypatch)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.FAILED.value
    assert catalog.requeues == 0  # terminal: no retry burned
    # The worker records the exception type, not its text (existing convention).
    assert result["error"]["type"] == "ExportBuildUnknown"


def test_export_build_unknown_is_the_terminal_type(tmp_path: Path) -> None:
    # Pins the worker's terminal set without a queue: the class exists so the
    # retry policy can tell "caller typo" from "disk hiccup".
    assert issubclass(ExportBuildUnknown, ValueError)


def test_missing_member_episode_fails_terminally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, blobs = _seeded_catalog(with_episodes=False)
    worker = _worker(tmp_path, catalog, blobs, monkeypatch=monkeypatch)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.FAILED.value
    assert catalog.requeues == 0
    assert result["error"]["type"] == "InvalidJobPayload"


def test_transient_read_failure_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog, blobs = _seeded_catalog()
    worker = _worker(tmp_path, catalog, blobs, monkeypatch=monkeypatch, fail_reads=True)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.QUEUED.value  # requeued for retry
    assert catalog.requeues == 1


def test_empty_payload_is_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog, blobs = _seeded_catalog()
    job = _export_job("bld_x")
    job["payload"] = {}
    catalog.job = job
    worker = _worker(tmp_path, catalog, blobs, monkeypatch=monkeypatch)

    result = worker.process_one()

    assert result is not None
    assert result["state"] == JobState.FAILED.value
    assert catalog.requeues == 0
