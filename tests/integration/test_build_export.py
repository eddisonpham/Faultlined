"""End-to-end export against real PostgreSQL: episodes → build → export → read back."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from data_engine.builds import DatasetBuilder
from data_engine.builds.export import FAULTLINED_DIR
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from data_engine.ingest.service import EpisodeIngestService
from data_engine.jobs.worker import IngestWorker
from tests.conftest import postgres_test_dsn

pytestmark = [pytest.mark.integration]


def _settings(tmp_path: Path) -> Settings | None:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("DE_DATABASE_URL is required for PostgreSQL integration tests")
    settings = Settings(
        database_url=dsn,
        artifact_root=tmp_path / "artifacts",
        export_root=tmp_path / "exports",
        _env_file=None,
    )
    initialize_schema(settings)
    return settings


def _ingest_episodes(catalog: PostgresCatalog, artifacts: Any, *, count: int) -> list[str]:
    service = EpisodeIngestService(catalog, artifacts)
    ids = []
    for i in range(count):
        result = service.ingest(
            {
                "task": f"task-{i % 2}",
                "robot": "so101",
                "timestamps": [round(0.1 * j, 3) for j in range(3 + i)],
                "observations": [[float(j), float(j) + 1] for j in range(3 + i)],
                "actions": [[float(j) * 2] for j in range(3 + i)],
            },
            job_id=f"job-ingest-{i}",
        )
        ids.append(str(result["episode_id"]))
    return sorted(ids)


def _export_job_payload(build_hash: str) -> dict[str, Any]:
    return {
        "id": "export-job-1",
        "type": "export",
        "state": "running",
        "correlation_id": "corr-export-1",
        "attempts": 1,
        "max_attempts": 1,
        "payload": {"build_hash": build_hash},
        "deadline_at": None,
    }


def test_build_export_round_trips_through_the_catalog_and_worker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings is not None
    catalog = PostgresCatalog(settings)
    from data_engine.storage.artifacts import FileArtifactStore

    store = FileArtifactStore(settings.artifact_root)
    episode_ids = _ingest_episodes(catalog, store, count=3)

    builder = DatasetBuilder(code_commit="testcommit1")
    build = builder.build(catalog.get_episodes(episode_ids), name="e2e-export-set")
    catalog.record_build(build, job_id="job-build-1")

    worker = IngestWorker(settings)
    worker.catalog = catalog
    job = _export_job_payload(build.hash)
    result = worker._run_handler(job, "export")

    assert result["build_hash"] == build.hash
    assert result["episode_count"] == 3
    root = settings.export_root / build.hash
    info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
    assert info["total_episodes"] == 3
    manifest = json.loads((root / FAULTLINED_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["episode_count"] == 3

    from data_engine.ingest.readers.lerobot import LeRobotReader

    reader = LeRobotReader()
    assert reader.sniff(root)
    read_back = [reader.read(root, episode_key=f"episode_index={i}") for i in range(3)]
    assert sorted(ep.frame_count for ep in read_back) == [3, 4, 5]
    assert all(ep.robot_type == "so101" for ep in read_back)
    rows = catalog.get_episodes(episode_ids)
    stored = sorted(int((row.get("metadata") or {}).get("frame_count") or 0) for row in rows)
    assert sorted(ep.frame_count for ep in read_back) == stored
    again = worker._run_handler(job, "export")
    assert again["path"] == result["path"]
    assert again["frame_count"] == result["frame_count"]
