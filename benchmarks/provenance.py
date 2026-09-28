"""Collect benchmark provenance without reading secrets or local environment files."""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from data_engine import __version__
from data_engine.observability.telemetry import sample_resources

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_provenance(
    config: dict[str, Any],
    *,
    workload: str,
    workload_version: str,
    dataset: str | None = "synthetic-episode-v1",
) -> dict[str, Any]:
    sample = sample_resources()
    hardware = {
        "cpu": platform.processor() or platform.machine(),
        "logical_cpus": psutil.cpu_count(logical=True) or 1,
        "ram_bytes": int(psutil.virtual_memory().total),
        "gpu": None,
        "disk": str(ROOT.anchor or ROOT.drive or "local filesystem"),
    }
    if sample.gpu_present:
        hardware["gpu"] = {
            "model": "NVIDIA GPU (NVML)",
            "memory_total_bytes": sample.gpu_memory_total_bytes,
            "driver": "available",
        }
    config_hash = hashlib.sha256(
        __import__("json").dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    lockfile = ROOT / "uv.lock"
    return {
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "config_hash": config_hash,
        "dataset": dataset,
        "dataset_version": "synthetic-episode-v1" if dataset else None,
        "model": None,
        "model_version": None,
        "hardware": hardware,
        "software": {
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "lockfile_hash": _hash_file(lockfile) if lockfile.exists() else "missing",
            "app_version": __version__,
        },
        "seeds": [20260928],
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "workload": workload,
        "workload_version": workload_version,
        "system_parameters": config,
        "background_load_note": (
            "No controlled background load; owner machine interactive, GPU may be active."
        ),
        "container_image_digest": None,
    }
