"""Write batching in the catalog: statement count must not scale with row count."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from data_engine.builds.service import DatasetBuilder
from data_engine.catalog.database import connect, initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings


def _dsn() -> str:
    from tests.conftest import postgres_test_dsn

    dsn = postgres_test_dsn()
    if not dsn:
        pytest.skip("DE_DATABASE_URL is required for PostgreSQL integration tests")
    return dsn


@pytest.fixture()
def catalog(tmp_path: Path) -> PostgresCatalog:
    settings = Settings(database_url=_dsn(), artifact_root=tmp_path, _env_file=None)
    initialize_schema(settings)
    return PostgresCatalog(settings)


def _episodes(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"batch_ep_{i:05d}",
            "episode_key": f"k{i}",
            "source_hash": f"src_{i:05d}",
            "artifact_hash": f"art_{i:05d}",
            "size_bytes": 128,
            "job_id": "job-batch",
            "episode_format": "synthetic-json",
            "metadata": {"frame_count": 10, "state": "valid"},
        }
        for i in range(count)
    ]


def _register(catalog: PostgresCatalog, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Register episodes and hand back catalog rows shaped for the builder."""
    return [
        catalog.register_episode(
            source_hash=row["source_hash"],
            artifact_hash=row["artifact_hash"],
            size_bytes=row["size_bytes"],
            metadata=row["metadata"],
            job_id=row["job_id"],
            episode_key=row["episode_key"],
            episode_format=row["episode_format"],
        )
        for row in rows
    ]


class _ConnectionProxy:
    """Delegates everything, counting the statements that are executed."""

    def __init__(self, connection: Any, seen: list[str]) -> None:
        self._connection = connection
        self._seen = seen

    def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        self._seen.append(str(query).strip().splitlines()[0].upper()[:60])
        return self._connection.execute(query, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count the statements the repository issues through `connect`."""
    seen: list[str] = []
    real_connect = connect

    @contextmanager
    def counting(settings: Any = None) -> Any:
        with real_connect(settings) as connection:
            yield _ConnectionProxy(connection, seen)

    monkeypatch.setattr("data_engine.catalog.repository.connect", counting)
    return seen


@pytest.mark.integration
def test_build_membership_write_does_not_scale_with_episode_count(
    catalog: PostgresCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes = _episodes(50)
    rows = _register(catalog, episodes)
    result = DatasetBuilder(code_commit="abc123").build(rows, name="batched")

    seen = _statements(monkeypatch)
    recorded = catalog.record_build(result)

    per_row = [
        s for s in seen if s.startswith(("INSERT INTO BUILD_EPISODES", "INSERT INTO LINEAGE_EDGES"))
    ]
    assert len(per_row) <= 2, f"membership writes are still per-row: {per_row[:6]}"

    members = catalog.build_episodes(recorded["hash"])
    assert len(members) == 50
    assert {m["episode_id"] for m in members} == {row["id"] for row in rows}


@pytest.mark.integration
def test_slice_membership_write_does_not_scale_with_episode_count(
    catalog: PostgresCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    for row in _episodes(20):
        catalog.register_episode(
            source_hash=row["source_hash"],
            artifact_hash=row["artifact_hash"],
            size_bytes=row["size_bytes"],
            metadata=row["metadata"],
            job_id=row["job_id"],
            episode_key=row["episode_key"],
            episode_format=row["episode_format"],
        )
    slice_id = catalog.register_slice(
        name=f"batch-slice-{uuid.uuid4()}", filter_config={"state": "valid"}, notes=""
    )["id"]

    seen = _statements(monkeypatch)
    manifest = catalog.slice_manifest(slice_id)

    per_row = [s for s in seen if "SLICE_MEMBERSHIPS" in s]
    assert len(per_row) <= 1, f"slice membership writes are still per-row: {per_row[:6]}"
    assert manifest is not None and manifest["count"] > 0
