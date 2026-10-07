"""Behavioural fingerprints over a real catalog (ADR 0032)."""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator

import pytest

from data_engine.analysis.fingerprint import redundancy_report
from data_engine.analysis.quality import analyze
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from tests.conftest import postgres_test_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
def catalog() -> Iterator[PostgresCatalog]:
    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("no DE_DATABASE_URL configured")
    settings = Settings(_env_file=None, database_url=dsn)  # type: ignore[arg-type]
    initialize_schema(settings)
    yield PostgresCatalog(settings)


def _seed(catalog: PostgresCatalog, series: dict[str, list[float]]) -> str:
    """Register one episode the way ingest does, quality included, and return its id."""
    token = uuid.uuid4().hex
    job, _ = catalog.submit_job("ingest", {}, f"fingerprint-{token}", "test-correlation")
    episode = catalog.register_episode(
        source_hash=token,
        artifact_hash=token,
        size_bytes=512,
        metadata={"task": "pick the cube", "robot": "so101"},
        job_id=str(job["id"]),
        episode_key=f"episode_{token[:8]}",
        episode_format="synthetic-json",
    )
    frames = len(next(iter(series.values())))
    timestamps = [index * 0.05 for index in range(frames)]
    catalog.record_episode_quality(
        str(episode["id"]), analyze(series, timestamps=timestamps).to_dict()
    )
    return str(episode["id"])


def _motion(points: int, *, period: float = 8.0, amplitude: float = 1.0) -> dict[str, list[float]]:
    return {
        "position[0]": [amplitude * math.sin(index / period) for index in range(points)],
        "position[1]": [amplitude * math.cos(index / period) for index in range(points)],
    }


def test_the_catalog_returns_the_signals_a_fingerprint_is_built_from(
    catalog: PostgresCatalog,
) -> None:
    episode_id = _seed(catalog, _motion(60))

    rows = catalog.episode_fingerprint_inputs([episode_id])

    assert len(rows) == 1
    row = rows[0]
    assert str(row["id"]) == episode_id
    assert row["task"] == "pick the cube"
    assert row["motion_trace"], "the trace analyze() wrote did not survive the round trip"
    assert [entry["name"] for entry in row["dims"]] == ["position[0]", "position[1]"]


def test_an_episode_that_was_never_scored_is_absent_rather_than_defaulted(
    catalog: PostgresCatalog,
) -> None:
    token = uuid.uuid4().hex
    job, _ = catalog.submit_job("ingest", {}, f"unscored-{token}", "test-correlation")
    episode = catalog.register_episode(
        source_hash=token,
        artifact_hash=token,
        size_bytes=8,
        metadata={"task": "pick"},
        job_id=str(job["id"]),
        episode_key=f"episode_{token[:8]}",
        episode_format="synthetic-json",
    )

    assert catalog.episode_fingerprint_inputs([str(episode["id"])]) == []


def test_two_recordings_of_the_same_motion_collapse_and_a_different_one_does_not(
    catalog: PostgresCatalog,
) -> None:
    """The measurement, at the smallest useful size, through the real writer and reader."""
    base = _seed(catalog, _motion(120))
    same_motion = _seed(catalog, _motion(120, amplitude=1.01))
    different = _seed(
        catalog,
        {
            "position[0]": [0.0 if index < 90 else 1.0 for index in range(120)],
            "position[1]": [0.0] * 120,
        },
    )

    rows = catalog.episode_fingerprint_inputs([base, same_motion, different])
    report = redundancy_report(rows)

    assert report.episode_count == 3
    assert report.distinct_count == 2
    assert report.redundant_count == 1
    collapsed = {group.representative for group in report.groups}
    assert different not in collapsed
    duplicate_ids = {item.episode_id for group in report.groups for item in group.duplicates}
    assert duplicate_ids == {base, same_motion} - collapsed


def test_a_fingerprint_input_request_for_no_episodes_asks_the_database_nothing(
    catalog: PostgresCatalog,
) -> None:
    assert catalog.episode_fingerprint_inputs([]) == []
