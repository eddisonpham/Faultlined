from unittest.mock import MagicMock

import pytest

from data_engine import cli


@pytest.mark.unit
def test_doctor_initializes_schema_and_reports_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    initialized = MagicMock()
    monkeypatch.setattr(cli, "initialize_schema", initialized)
    cli.main(["doctor"])
    initialized.assert_called_once()
    assert "PostgreSQL connection and catalog schema: OK" in capsys.readouterr().out


@pytest.mark.unit
def test_gc_reports_placeholder(capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["gc"])
    assert "No garbage-collection work" in capsys.readouterr().out


@pytest.mark.unit
def test_worker_once_initializes_and_processes_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "initialize_schema", MagicMock())
    worker = MagicMock()
    monkeypatch.setattr(cli, "IngestWorker", lambda _settings: worker)
    cli.main(["worker", "--once"])
    worker.process_one.assert_called_once()


@pytest.mark.unit
def test_api_command_runs_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "initialize_schema", MagicMock())
    run = MagicMock()
    monkeypatch.setattr(cli.uvicorn, "run", run)
    cli.main(["api"])
    run.assert_called_once()


@pytest.mark.unit
def test_dev_starts_and_stops_worker_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "initialize_schema", MagicMock())
    process = MagicMock()
    process.is_alive.return_value = False
    monkeypatch.setattr(cli.multiprocessing, "Process", lambda **_kwargs: process)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *_args, **_kwargs: None)
    cli.main(["dev"])
    process.start.assert_called_once()
    process.join.assert_called_once()
