"""The `just reset` data wipe.

Two of its behaviours are load-bearing and neither is visible in normal use: it
refuses a `*_test` database, and it refuses to run without confirmation. Both are
asserted here, because a reset script that quietly grows a default is a script
that eventually deletes something it should not.
"""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reset_data.py"
_SPEC = importlib.util.spec_from_file_location("reset_data", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_RESET = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RESET)


def _settings(monkeypatch: pytest.MonkeyPatch, dsn: str) -> Any:
    from data_engine.config import Settings

    monkeypatch.setenv("DE_DATABASE_URL", dsn)
    return Settings(_env_file=None)


@pytest.mark.unit
def test_it_refuses_a_test_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite owns `*_test`; a reset must never be a way to lose a test run."""
    monkeypatch.setenv("DE_DATABASE_URL", "postgresql://localhost:5432/data_engine_test")
    with pytest.raises(SystemExit, match="refusing to reset"):
        _RESET.main(["--yes"])


@pytest.mark.unit
def test_it_refuses_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`just reset` is muscle memory by now. The confirmation has to be typed."""
    monkeypatch.setenv("DE_DATABASE_URL", "postgresql://localhost:5432/data_engine")
    with pytest.raises(SystemExit, match="--yes"):
        _RESET.main([])


@pytest.mark.unit
def test_keep_artifacts_leaves_the_blob_store_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, "postgresql://localhost:5432/data_engine")
    monkeypatch.setattr(_RESET, "load_settings", lambda: settings)
    monkeypatch.setattr(_RESET, "wipe_catalog", lambda _dsn: ["episodes"])
    store = tmp_path / "artifacts"
    (store / "blobs" / "sha256" / "ab").mkdir(parents=True)
    (store / "blobs" / "sha256" / "ab" / "deadbeef").write_bytes(b"x")
    settings.artifact_root = store

    _RESET.main(["--yes", "--keep-artifacts"])

    assert (store / "blobs" / "sha256" / "ab" / "deadbeef").is_file()


@pytest.mark.unit
def test_a_full_reset_removes_blobs_and_the_metrics_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch, "postgresql://localhost:5432/data_engine")
    monkeypatch.setattr(_RESET, "load_settings", lambda: settings)
    monkeypatch.setattr(_RESET, "wipe_catalog", lambda _dsn: ["episodes", "jobs"])
    store = tmp_path / "artifacts"
    (store / "blobs" / "sha256" / "ab").mkdir(parents=True)
    (store / "blobs" / "sha256" / "ab" / "deadbeef").write_bytes(b"x")
    metrics = tmp_path / "metrics" / "runtime.jsonl"
    metrics.parent.mkdir(parents=True)
    metrics.write_text('{"name":"x"}\n', encoding="utf-8")
    settings.artifact_root = store
    settings.metrics_path = metrics

    _RESET.main(["--yes"])

    assert not store.exists()
    assert not metrics.exists()


@pytest.mark.unit
def test_catalog_tables_comes_from_pg_tables_not_a_hardcoded_list() -> None:
    """A hardcoded list silently stops covering new tables; the catalog is the truth."""

    class _Cursor:
        def __init__(self) -> None:
            self.sql = ""

        def execute(self, sql: str) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[str]]:
            return [("builds",), ("episodes",), ("jobs",)]

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    cursor = _Cursor()
    assert _RESET.catalog_tables(cursor) == ["builds", "episodes", "jobs"]  # type: ignore[arg-type]
    assert "pg_tables" in cursor.sql
