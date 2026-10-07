from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest

from data_engine.catalog.repository import (
    IdempotencyConflict,
    InvalidTransition,
    PostgresCatalog,
)
from data_engine.jobs.state import JobState


class ScriptedConnection:
    def __init__(self, results: list[dict[str, Any] | None]) -> None:
        self.results = iter(results)
        self.statements: list[str] = []
        self.current: dict[str, Any] | None = None

    def execute(self, query: str, params: Any = None) -> ScriptedConnection:
        self.statements.append(query)
        self.current = next(self.results)
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self.current

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.current) if isinstance(self.current, list) else []


@contextmanager
def _connection(connection: ScriptedConnection) -> Iterator[ScriptedConnection]:
    yield connection


_PATCHERS: list[Any] = []


@pytest.fixture(autouse=True)
def _stop_patches() -> Iterator[None]:
    yield
    while _PATCHERS:
        _PATCHERS.pop().stop()


def _catalog_with(*results: dict[str, Any] | None) -> tuple[PostgresCatalog, ScriptedConnection]:
    connection = ScriptedConnection(list(results))
    catalog = PostgresCatalog()
    patcher = patch("data_engine.catalog.repository.connect", return_value=_connection(connection))
    patcher.start()
    _PATCHERS.append(patcher)
    return catalog, connection


@pytest.mark.unit
def test_submit_job_inserts_queued_record() -> None:
    inserted = {"id": "job-1", "state": "queued"}
    catalog, connection = _catalog_with(inserted)

    job, created = catalog.submit_job("ingest", {"episode": {}}, None, "corr-1")

    assert created is True
    assert job["id"] == "job-1"
    assert len(connection.statements) == 1


@pytest.mark.unit
def test_submit_job_replays_same_idempotency_key() -> None:
    existing = {"id": "job-1", "request_hash": "same"}
    catalog, _ = _catalog_with(existing)
    with patch("data_engine.catalog.repository.hashlib.sha256") as sha:
        sha.return_value.hexdigest.return_value = "same"
        job, created = catalog.submit_job("ingest", {"episode": {}}, "key", "corr")

    assert created is False
    assert job["id"] == "job-1"


@pytest.mark.unit
def test_submit_job_rejects_key_reuse_for_different_request() -> None:
    catalog, _ = _catalog_with({"id": "job-1", "request_hash": "different"})
    with pytest.raises(IdempotencyConflict):
        catalog.submit_job("ingest", {"episode": {}}, "key", "corr")


@pytest.mark.unit
def test_submit_job_handles_concurrent_idempotency_insert() -> None:
    existing = {"id": "job-1", "request_hash": "same"}
    catalog, _ = _catalog_with(None, None, existing)
    with patch("data_engine.catalog.repository.hashlib.sha256") as sha:
        sha.return_value.hexdigest.return_value = "same"
        job, created = catalog.submit_job("ingest", {}, "key", "corr")

    assert created is False
    assert job["id"] == "job-1"


@pytest.mark.unit
def test_submit_job_detects_concurrent_key_conflict() -> None:
    catalog, _ = _catalog_with(None, None, {"id": "job-1", "request_hash": "other"})
    with pytest.raises(IdempotencyConflict):
        catalog.submit_job("ingest", {}, "key", "corr")


@pytest.mark.unit
def test_claim_job_transitions_queued_to_running() -> None:
    catalog, _ = _catalog_with(
        {"id": "job-1", "state": "queued"}, {"id": "job-1", "state": "running"}
    )
    job = catalog.claim_job()
    assert job is not None
    assert job["state"] == "running"


@pytest.mark.unit
def test_claim_job_returns_none_when_queue_is_empty() -> None:
    catalog, _ = _catalog_with(None)
    assert catalog.claim_job() is None


@pytest.mark.unit
def test_claim_job_fails_if_claimed_row_disappears() -> None:
    catalog, _ = _catalog_with({"id": "job-1", "state": "queued"}, None)
    with pytest.raises(RuntimeError, match="disappeared"):
        catalog.claim_job()


@pytest.mark.unit
def test_finish_job_enforces_state_transition() -> None:
    catalog, _ = _catalog_with(
        {"id": "job-1", "state": "running"}, {"id": "job-1", "state": "succeeded"}
    )
    result = catalog.finish_job("job-1", JobState.SUCCEEDED, result={"ok": True})
    assert result["state"] == "succeeded"


@pytest.mark.unit
def test_finish_job_rejects_terminal_transition_and_missing_job() -> None:
    catalog, _ = _catalog_with({"id": "job-1", "state": "succeeded"})
    with pytest.raises(InvalidTransition):
        catalog.finish_job("job-1", JobState.FAILED)
    missing, _ = _catalog_with(None)
    with pytest.raises(KeyError):
        missing.finish_job("missing", JobState.FAILED)


@pytest.mark.unit
def test_finish_job_fails_if_update_returns_no_record() -> None:
    catalog, _ = _catalog_with({"id": "job-1", "state": "running"}, None)
    with pytest.raises(RuntimeError, match="transition"):
        catalog.finish_job("job-1", JobState.FAILED)


@pytest.mark.unit
def test_register_episode_persists_artifact_and_lineage() -> None:
    episode = {"id": "episode-1", "source_hash": "hash", "artifact_hash": "hash"}
    catalog, connection = _catalog_with(None, episode, None)
    actual = catalog.register_episode(
        source_hash="hash",
        artifact_hash="hash",
        size_bytes=10,
        metadata={"task": "pick"},
        job_id="job-1",
    )
    assert actual["id"] == "episode-1"
    assert len(connection.statements) == 3


@pytest.mark.unit
def test_register_episode_fails_if_upsert_returns_no_record() -> None:
    catalog, _ = _catalog_with(None, None)
    with pytest.raises(RuntimeError, match="no row"):
        catalog.register_episode(
            source_hash="hash", artifact_hash="hash", size_bytes=1, metadata={}, job_id="job-1"
        )


@pytest.mark.unit
def test_episode_fingerprint_inputs_projects_only_the_signals_it_reads() -> None:
    """`dims` and `motion_trace` are the largest jsonb values the catalog stores, so the
    projection is deliberate: a report over a build of thousands must not drag every
    episode's full metadata and history along with two columns it will not look at."""
    catalog, connection = _catalog_with([{"id": "episode-1"}])
    rows = catalog.episode_fingerprint_inputs(["episode-1"])

    assert rows == [{"id": "episode-1"}]
    query = connection.statements[0].lower()
    for column in ("q.dims", "q.motion_trace", "q.verdict", "q.judged_dims", "q.stall_ratio"):
        assert column in query
    assert "select *" not in query
    assert "q.episode_id" not in query.split("from")[0]  # the join key is not a result column


@pytest.mark.unit
def test_episode_fingerprint_inputs_asks_nothing_for_an_empty_set() -> None:
    catalog, connection = _catalog_with()
    assert catalog.episode_fingerprint_inputs([]) == []
    assert connection.statements == []


@pytest.mark.unit
def test_get_job_and_episode_return_none_or_record() -> None:
    catalog, _ = _catalog_with({"id": "job-1"})
    assert catalog.get_job("job-1")["id"] == "job-1"
    missing, _ = _catalog_with(None)
    assert missing.get_job("missing") is None
    episode_catalog, _ = _catalog_with({"id": "episode-1"}, [])
    episode = episode_catalog.get_episode("episode-1")
    assert episode is not None
    assert episode["id"] == "episode-1"
    assert episode["lineage"] == []
    missing_episode, _ = _catalog_with(None)
    assert missing_episode.get_episode("missing") is None
