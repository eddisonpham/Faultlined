"""Unit tests for the migration runner (ADR 0028)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from psycopg import Connection

from data_engine import cli
from data_engine.catalog import migrations
from data_engine.catalog.migrations import BaselineMigration, Migration, compute_pending


class _FakeMigration:
    def __init__(self, version: str, name: str = "probe") -> None:
        self.version = version
        self.name = name
        self.calls = 0

    def apply(self, connection: Connection[object]) -> None:
        self.calls += 1


def _registry(*versions: str) -> tuple[Migration, ...]:
    return tuple(_FakeMigration(version) for version in versions)


def test_compute_pending_skips_applied_and_keeps_registry_order() -> None:
    registry = _registry("0001", "0002", "0003")
    pending = compute_pending(["0001"], registry)
    assert [m.version for m in pending] == ["0002", "0003"]


def test_compute_pending_allows_gaps_in_the_recorded_history() -> None:
    """A withdrawn draft must not force a database to be marked unapplied."""
    registry = _registry("0001", "0002", "0003")
    pending = compute_pending(["0001", "0003"], registry)
    assert [m.version for m in pending] == ["0002"]


def test_compute_pending_empty_applied_returns_everything() -> None:
    assert [m.version for m in compute_pending([], _registry("0001", "0002"))] == ["0001", "0002"]


def test_compute_pending_refuses_duplicate_versions_in_the_registry() -> None:
    first, second = _FakeMigration("0001"), _FakeMigration("0001")
    with pytest.raises(ValueError, match="duplicate migration version '0001'"):
        compute_pending([], (first, second))  # type: ignore[arg-type]


def test_unknown_versions_names_what_the_registry_does_not_know() -> None:
    registry = _registry("0001")
    assert migrations.unknown_versions(["0001", "0009"], registry) == ["0009"]
    assert migrations.unknown_versions(["0001"], registry) == []


def test_the_shipped_registry_is_ordered_and_carries_only_real_changes() -> None:
    versions = [m.version for m in migrations.REGISTRY]
    assert versions == sorted(versions)
    assert versions[0] == "0001", "the baseline is always first"
    assert versions == ["0001", "0002"], (
        "no migration ships without a real change behind it; 0002 is the "
        "task-vocabulary backfill (ADR 0029)"
    )


def test_baseline_migration_runs_the_schema_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(migrations, "run_schema_ddl", lambda connection: calls.append(connection))
    baseline = BaselineMigration()
    sentinel = object()
    baseline.apply(sentinel)  # type: ignore[arg-type]
    assert calls == [sentinel]


def test_migrate_status_prints_and_exits_nonzero_when_the_database_is_ahead(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        migrations,
        "status",
        lambda _settings, **_k: {
            "applied": ["0001", "0002"],
            "pending": [],
            "unknown": ["0002"],
        },
    )
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "configure_logging", lambda *_a: None)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["migrate", "--status"])
    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "applied: 0001, 0002" in out
    assert "newer than this code" in out


def test_migrate_status_with_nothing_unknown_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        migrations,
        "status",
        lambda _settings, **_k: {"applied": ["0001"], "pending": [], "unknown": []},
    )
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "configure_logging", lambda *_a: None)
    cli.main(["migrate", "--status"])
    out = capsys.readouterr().out
    assert "pending: (none)" in out
    assert "unknown: (none)" in out


def test_migrate_applies_and_reports_what_it_applied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(migrations, "upgrade", lambda _settings, **_k: ["0001", "0002"])
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "configure_logging", lambda *_a: None)
    cli.main(["migrate"])
    assert "applied: 0001, 0002" in capsys.readouterr().out


def test_migrate_with_nothing_pending_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(migrations, "upgrade", lambda _settings, **_k: [])
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "configure_logging", lambda *_a: None)
    cli.main(["migrate"])
    assert "nothing to apply" in capsys.readouterr().out
