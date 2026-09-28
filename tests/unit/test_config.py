from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from data_engine.config import Settings


@pytest.mark.unit
def test_settings_defaults_and_artifact_root_expansion() -> None:
    settings = Settings(_env_file=None)
    assert settings.api_host == "127.0.0.1"
    assert settings.api_port == 8000
    assert settings.log_format == "json"
    assert isinstance(settings.database_url, SecretStr)
    assert settings.artifact_root == Path("var/artifacts")


@pytest.mark.unit
def test_worker_slots_auto_uses_bounded_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 24)
    settings = Settings(worker_slots="auto", _env_file=None)
    assert settings.worker_slot_counts() == (8, 1)


@pytest.mark.unit
def test_worker_slots_explicit_values() -> None:
    settings = Settings(worker_slots="cpu=4,gpu=0", _env_file=None)
    assert settings.worker_slot_counts() == (4, 0)


@pytest.mark.unit
def test_settings_read_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DE_API_PORT", "8123")
    settings = Settings(_env_file=None)
    assert settings.api_port == 8123


@pytest.mark.unit
def test_settings_reject_invalid_port() -> None:
    with pytest.raises(ValidationError):
        Settings(api_port=70000, _env_file=None)
