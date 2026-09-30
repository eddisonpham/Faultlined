"""Typed settings loaded from process environment variables only (ADR 0002).

The local task runner may import a gitignored .env into its process environment; application
settings never open .env. Secrets (HF_KEY, database URL) are never logged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for all components; validated at startup."""

    model_config = SettingsConfigDict(env_prefix="DE_", extra="ignore")

    database_url: SecretStr = SecretStr("postgresql://localhost:5432/data_engine")
    artifact_root: Path = Path("./var/artifacts")
    export_root: Path = Path("./var/exports")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    worker_slots: str = "auto"  # "auto" or "cpu=N,gpu=N"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    metrics_path: Path = Path("./var/metrics/runtime.jsonl")

    # No env_prefix for this one: the name is fixed by the secrets convention (ADR 0002).
    hf_key: SecretStr | None = Field(default=None, validation_alias="HF_KEY")

    @field_validator("artifact_root")
    @classmethod
    def _expand_artifact_root(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("export_root")
    @classmethod
    def _expand_export_root(cls, value: Path) -> Path:
        return value.expanduser()

    def worker_slot_counts(self) -> tuple[int, int]:
        """Return (cpu_slots, gpu_slots); 'auto' resolves cpu_slots from the machine."""
        if self.worker_slots == "auto":
            import os

            cpu_count = os.cpu_count() or 4
            return (max(2, min(8, cpu_count // 2)), 1)
        cpu_part, gpu_part = self.worker_slots.split(",", maxsplit=1)
        return (int(cpu_part.removeprefix("cpu=")), int(gpu_part.removeprefix("gpu=")))


def load_settings() -> Settings:
    """Load settings at startup; pydantic raises typed errors on invalid configuration (F3)."""
    return Settings()
