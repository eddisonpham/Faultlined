"""Real LeRobot ingest, end to end, against a real database.

The unit tests prove the reader is internally consistent. This proves the *pipeline*
is: a genuine Hub dataset goes through the API contract, the job queue, the worker,
the artifact store, and the catalog, and comes back as a queryable episode with
correct lineage.

Marked `network`: the dataset is fetched once into gitignored `var/real-data/` and
reused after that, so a second run is offline. It skips cleanly if the hub is
unreachable, because a laptop on a train still has to run the suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from data_engine.jobs.state import JobState
from data_engine.jobs.worker import IngestWorker
from tests.conftest import postgres_test_dsn

pytestmark = [pytest.mark.integration, pytest.mark.network]


def _settings(tmp_path: Path) -> Settings:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("DE_DATABASE_URL is required for PostgreSQL integration tests")
    settings = Settings(database_url=dsn, artifact_root=tmp_path, _env_file=None)
    initialize_schema(settings)
    return settings


def _run(catalog: PostgresCatalog, worker: IngestWorker, job_id: str, limit: int = 100) -> Any:
    """Drain the shared queue until this job leaves it, then return its row.

    The test database is shared with the rest of the suite, so the worker claims the
    oldest queued job, not necessarily the one under test.
    """
    for _ in range(limit):
        row = catalog.get_job(job_id)
        if row is None or row["state"] != JobState.QUEUED.value:
            break
        if worker.process_one() is None:
            break
    return catalog.get_job(job_id)


@pytest.mark.integration
def test_real_v3_dataset_ingests_through_the_pipeline(
    tmp_path: Path, real_lerobot_dataset: Callable[[str], Path]
) -> None:
    """lerobot/svla_so101_pickplace episode 7, real bytes, real Postgres."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    root = real_lerobot_dataset("v3")
    job, created = catalog.submit_job(
        "ingest_source",
        {"source": str(root), "episode_key": "episode_index=7"},
        f"real-v3-{uuid.uuid4()}",
        "test-correlation",
    )
    assert created is True

    worker = IngestWorker(settings)
    finished = _run(catalog, worker, job["id"])

    assert finished["state"] == JobState.SUCCEEDED.value, finished.get("error")
    result = finished["result"]
    assert result["format"] == "lerobot-v3"
    assert result["episode_key"] == "episode_index=7"
    assert result["frame_count"] > 0

    episode = catalog.get_episode(result["episode_id"])
    assert episode is not None
    assert episode["format"] == "lerobot-v3"
    assert episode["episode_key"] == "episode_index=7"
    assert episode["metadata"]["robot"] == "so100_follower"
    assert episode["metadata"]["frame_count"] == result["frame_count"]
    assert episode["metadata"]["dataset"]["codebase_version"] == "v3.0"
    assert "action" in episode["metadata"]["channel_stats"]

    # The artifact is the file the rows came from, and it round-trips byte for byte.
    assert (
        worker.artifacts.get_bytes(episode["artifact_hash"])
        == (root / "data" / "chunk-000" / "file-000.parquet").read_bytes()
    )

    # And the episode records which job produced it.
    assert job["id"] in [edge["to_ref"] for edge in episode["lineage"]]


@pytest.mark.integration
def test_two_episodes_from_one_file_become_two_rows(
    tmp_path: Path, real_lerobot_dataset: Callable[[str], Path]
) -> None:
    """A v3 Parquet shard holds many episodes; identity is (file, episode), not file."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    root = real_lerobot_dataset("v3")
    worker = IngestWorker(settings)

    submitted = []
    for episode in (0, 1):
        job, _ = catalog.submit_job(
            "ingest_source",
            {"source": str(root), "episode_key": f"episode_index={episode}"},
            f"real-v3-pair-{uuid.uuid4()}",
            "test-correlation",
        )
        submitted.append((episode, job["id"]))
    for _, job_id in submitted:
        assert _run(catalog, worker, job_id)["state"] == JobState.SUCCEEDED.value

    rows = [catalog.get_job(job_id)["result"] for _, job_id in submitted]
    assert rows[0]["artifact_hash"] == rows[1]["artifact_hash"], "one file, one address"
    assert rows[0]["episode_id"] != rows[1]["episode_id"]
    assert rows[0]["frame_count"] != rows[1]["frame_count"], "distinct row ranges"


@pytest.mark.integration
def test_reingesting_the_same_episode_is_idempotent(
    tmp_path: Path, real_lerobot_dataset: Callable[[str], Path]
) -> None:
    """The catalog upserts on (source_hash, episode_key); a repeat is not a duplicate."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    root = real_lerobot_dataset("v3")
    worker = IngestWorker(settings)

    job_ids = []
    for _ in range(2):
        job, _ = catalog.submit_job(
            "ingest_source",
            {"source": str(root), "episode_key": "episode_index=3"},
            f"real-v3-idem-{uuid.uuid4()}",
            "test-correlation",
        )
        job_ids.append(job["id"])
    for job_id in job_ids:
        assert _run(catalog, worker, job_id)["state"] == JobState.SUCCEEDED.value

    episodes = {catalog.get_job(job_id)["result"]["episode_id"] for job_id in job_ids}
    assert len(episodes) == 1, "the same episode from the same file is one catalog row"


@pytest.mark.integration
def test_unreadable_source_fails_without_retrying(
    tmp_path: Path, real_lerobot_dataset: Callable[[str], Path]
) -> None:
    """F1: bad data is terminal. Retrying identical bytes cannot change the answer."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    root = real_lerobot_dataset("v3")
    job, _ = catalog.submit_job(
        "ingest_source",
        {"source": str(root), "episode_key": "episode_index=9999"},
        f"real-v3-bad-{uuid.uuid4()}",
        "test-correlation",
        max_attempts=3,
    )

    finished = _run(catalog, IngestWorker(settings), job["id"])

    assert finished["attempts"] == 1, "an unparseable episode must not burn the budget"
    assert finished["state"] == JobState.FAILED.value
    assert finished["error"]["type"] == "ReaderError"


def _ingest_one(
    catalog: PostgresCatalog, worker: IngestWorker, source: Path, episode_key: str
) -> dict[str, Any]:
    job, _ = catalog.submit_job(
        "ingest_source",
        {"source": str(source), "episode_key": episode_key},
        f"real-validate-{uuid.uuid4()}",
        "test-correlation",
    )
    finished = _run(catalog, worker, job["id"])
    assert finished["state"] == JobState.SUCCEEDED.value, finished.get("error")
    return finished["result"]


def _validate(
    catalog: PostgresCatalog, worker: IngestWorker, episode_id: str, profile: dict[str, Any]
) -> dict[str, Any]:
    job, _ = catalog.submit_job(
        "validate",
        {"episode_id": episode_id, "profile": profile},
        f"real-validate-{uuid.uuid4()}",
        "test-correlation",
    )
    finished = _run(catalog, worker, job["id"])
    assert finished["state"] == JobState.SUCCEEDED.value, finished.get("error")
    return finished["result"]


def _results_for(catalog: PostgresCatalog, episode_id: str, profile_name: str) -> list[Any]:
    """Only the results this test's profile produced.

    Episode identity is content-addressed, so the same dataset yields the same catalog
    row on every run and the test database accumulates results across runs. Asserting
    on *all* results for an episode would make the test depend on how many times it had
    been run before, which is exactly the kind of statefulness this suite avoids.
    """
    return [
        row
        for row in catalog.get_validation_results(episode_id)
        if row["profile_name"] == profile_name
    ]


@pytest.mark.integration
def test_real_episode_passes_a_profile_that_matches_its_shape(
    tmp_path: Path, real_lerobot_dataset: Callable[[str], Path]
) -> None:
    """The published SO-101 dataset must validate cleanly against its own profile."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    worker = IngestWorker(settings)
    result = _ingest_one(catalog, worker, real_lerobot_dataset("v3"), "episode_index=0")

    outcome = _validate(
        catalog,
        worker,
        result["episode_id"],
        {
            "name": "so101-manipulation",
            "version": "1",
            "required_channels": ["action", "observation.state"],
            "min_frames": 100,
            "max_fps": 60,
            "min_fps": 1,
        },
    )

    assert outcome["passed"] is True
    assert outcome["reason_codes"] == []
    episode = catalog.get_episode(result["episode_id"])
    assert episode["state"] == "valid"
    results = _results_for(catalog, result["episode_id"], "so101-manipulation")
    assert len(results) == 1
    assert results[0]["passed"] is True


@pytest.mark.integration
def test_a_mismatched_profile_quarantines_the_episode_with_reasons(
    tmp_path: Path, real_lerobot_dataset: Callable[[str], Path]
) -> None:
    """F2: quarantine records *why*, and the bytes are not re-ingested to find out."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    worker = IngestWorker(settings)
    result = _ingest_one(catalog, worker, real_lerobot_dataset("v3"), "episode_index=0")

    outcome = _validate(
        catalog,
        worker,
        result["episode_id"],
        {"name": "too-strict", "version": "1", "required_channels": ["force_torque"]},
    )

    assert outcome["passed"] is False
    assert "VALIDATION_FAILED" in outcome["reason_codes"]
    assert "REQUIRED_CHANNEL_MISSING" in outcome["reason_codes"]
    episode = catalog.get_episode(result["episode_id"])
    assert episode["state"] == "quarantined"
    violations = _results_for(catalog, result["episode_id"], "too-strict")[0]["violations"]
    assert [v["channel"] for v in violations] == ["force_torque"]


@pytest.mark.integration
def test_remediation_revalidates_without_reingesting(
    tmp_path: Path, real_lerobot_dataset: Callable[[str], Path]
) -> None:
    """FR-003: a fixed profile produces a second result; the artifact is not re-read."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    worker = IngestWorker(settings)
    result = _ingest_one(catalog, worker, real_lerobot_dataset("v3"), "episode_index=0")
    artifact_hash = result["artifact_hash"]

    strict = {"name": "staged-strict", "version": "1", "required_channels": ["force_torque"]}
    assert _validate(catalog, worker, result["episode_id"], strict)["passed"] is False
    relaxed = {"name": "staged-relaxed", "version": "2", "required_channels": ["action"]}
    assert _validate(catalog, worker, result["episode_id"], relaxed)["passed"] is True

    episode = catalog.get_episode(result["episode_id"])
    assert episode["state"] == "valid"
    assert episode["artifact_hash"] == artifact_hash, "no re-ingest should have happened"
    assert _results_for(catalog, result["episode_id"], "staged-strict")[0]["passed"] is False
    assert _results_for(catalog, result["episode_id"], "staged-relaxed")[0]["passed"] is True
    hashes = {
        row["profile_hash"]
        for name in ("staged-strict", "staged-relaxed")
        for row in _results_for(catalog, result["episode_id"], name)
    }
    assert len(hashes) == 2, "two profiles are two different policies"


@pytest.mark.integration
def test_real_episodes_carry_quality_signals(
    tmp_path: Path, real_lerobot_dataset: Callable[[str], Path]
) -> None:
    """Motion quality lands with the episode: no second pass over the bytes."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    worker = IngestWorker(settings)
    result = _ingest_one(catalog, worker, real_lerobot_dataset("v3"), "episode_index=7")

    quality = catalog.get_episode_quality(result["episode_id"])
    assert quality is not None
    assert quality["frame_count"] == result["frame_count"]
    assert quality["movement_score"] > 0
    assert 0.0 <= quality["stall_ratio"] <= 1.0
    assert quality["verdict"] in {"smooth", "moderate", "jerky", "unknown"}
    # A 6-dim arm expands into named per-joint dims.
    assert "action[0]" in {dim["name"] for dim in quality["dims"]}
    assert quality["length_zscore"] == quality["length_zscore"]  # finite, not NaN

    summary = catalog.quality_summary()
    assert summary["episode_count"] >= 1
    assert summary["length"]["count"] >= 1
    assert any(item["episode_id"] == result["episode_id"] for item in summary["speed_distribution"])


@pytest.mark.integration
def test_lineage_and_validation_are_queryable_after_the_run(
    tmp_path: Path, real_lerobot_dataset: Callable[[str], Path]
) -> None:
    """Run inspection surfaces: what a job produced, and why an episode is quarantined."""
    settings = _settings(tmp_path)
    catalog = PostgresCatalog(settings)
    worker = IngestWorker(settings)
    job, _ = catalog.submit_job(
        "ingest_source",
        {"source": str(real_lerobot_dataset("v3")), "episode_key": "episode_index=7"},
        f"lineage-{uuid.uuid4()}",
        "test-correlation",
    )
    finished = _run(catalog, worker, job["id"])
    assert finished["state"] == JobState.SUCCEEDED.value, finished.get("error")
    episode_id = finished["result"]["episode_id"]

    produced = catalog.episodes_produced_by(job["id"])
    assert [row["id"] for row in produced] == [episode_id]
    assert produced[0]["episode_key"] == "episode_index=7"
    assert produced[0]["source_hash"] and produced[0]["artifact_hash"]
    assert catalog.episodes_produced_by("no-such-job") == []

    profile_name = f"lineage-strict-{uuid.uuid4()}"
    _validate(
        catalog,
        worker,
        episode_id,
        {
            "name": profile_name,
            "version": "1",
            "min_frames": 1_000_000,
            "enabled_rules": ["frame_count"],
        },
    )
    results = _results_for(catalog, episode_id, profile_name)
    assert len(results) == 1
    assert results[0]["passed"] is False
    assert "TOO_FEW_FRAMES" in results[0]["reason_codes"]
    assert catalog.get_episode(episode_id)["state"] == "quarantined"

    report = catalog.job_report(job["id"])
    assert report is not None
    assert report["episodes"]["total"] == 1
    assert report["episodes"]["by_state"].get("quarantined") == 1
    assert report["episodes"]["verdicts"]
    assert report["validation"]["failed"] >= 1
    assert "TOO_FEW_FRAMES" in report["validation"]["reason_codes"]
    assert catalog.job_report("no-such-job") is None

    rows = catalog.list_episodes(limit=500, state="quarantined")
    assert any(row["id"] == episode_id and row["artifact_hash"] for row in rows)
