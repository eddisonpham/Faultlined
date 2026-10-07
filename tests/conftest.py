"""Shared test helpers."""

from __future__ import annotations

import http.client
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from data_engine.observability.telemetry import TelemetrySample

TEST_DB_SUFFIX = "_test"
REAL_DATA_ROOT = Path("var") / "real-data"
HUB = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_ATTEMPTS = 6
DOWNLOAD_BACKOFF_SECONDS = 1.0
USER_AGENT = (
    "Mozilla/5.0 (compatible; faultlined-test-fixtures/0.1; "
    "+https://github.com/eddisonpham/Faultlined)"
)
"""The Hub's CDN resets connections from non-browser agents for some repositories."""

REAL_LEROBOT_FIXTURES = {
    "v3": (
        "lerobot/svla_so101_pickplace",
        (
            "meta/info.json",
            "meta/stats.json",
            "meta/tasks.parquet",
            "meta/episodes/chunk-000/file-000.parquet",
            "data/chunk-000/file-000.parquet",
        ),
    ),
    "v2": (
        "yaak-ai/lerobot-driving-school",
        (
            "meta/info.json",
            "meta/tasks.jsonl",
            "meta/episodes.jsonl",
            "data/chunk-000/episode_000000.parquet",
        ),
    ),
}


def _ensure_database(info: dict[str, str], dbname: str) -> None:
    with psycopg.connect(
        make_conninfo(**{**info, "dbname": "postgres"}), autocommit=True
    ) as maintenance:
        exists = maintenance.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if exists is None:
            maintenance.execute(f'CREATE DATABASE "{dbname}"')


def postgres_test_dsn() -> str:
    """DSN for the test database, or empty when no database is configured."""
    base = os.environ.get("DE_DATABASE_URL")
    if not base:
        return ""
    info = conninfo_to_dict(base)
    dbname = info.get("dbname") or "data_engine"
    test_dbname = f"{dbname}{TEST_DB_SUFFIX}"
    _ensure_database(info, test_dbname)
    return make_conninfo(**{**info, "dbname": test_dbname})


def pin_a_healthy_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the host sample the monitor reads, for tests that assert on signals."""
    monkeypatch.setattr(
        "data_engine.monitoring.service.sample_resources",
        lambda **_kwargs: TelemetrySample(
            timestamp="2026-09-29T12:00:00+00:00",
            cpu_percent=11.0,
            process_rss_bytes=1024**3,
            memory_used_bytes=8 * 1024**3,
            memory_available_bytes=24 * 1024**3,
            disk_used_bytes=200 * 1024**3,
            disk_free_bytes=500 * 1024**3,
            network_bytes_sent_total=0,
            network_bytes_recv_total=0,
            gpu_present=False,
        ),
    )


class FixtureUnavailable(RuntimeError):
    """A real-data fixture could not be fetched intact."""


def _download(url: str, target: Path) -> None:
    """Fetch one file, retrying transient resets."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".partial")
    last: Exception | None = None
    for _ in range(DOWNLOAD_ATTEMPTS):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Connection": "close"}
            )
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                declared = response.headers.get("Content-Length")
                try:
                    body = response.read()
                except http.client.IncompleteRead as exc:
                    raise OSError(f"truncated body: {exc}") from exc
            if declared is not None and len(body) != int(declared):
                raise OSError(f"truncated body: {len(body)} of {declared} bytes")
            temporary.write_bytes(body)
        except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
            last = exc
            time.sleep(DOWNLOAD_BACKOFF_SECONDS)
            continue
        temporary.replace(target)
        return
    temporary.unlink(missing_ok=True)
    raise FixtureUnavailable(f"{url}: {last}")


@pytest.fixture(scope="session")
def real_lerobot_dataset() -> Callable[[str], Path]:
    """A callable returning the root of a downloaded real LeRobot dataset."""

    def fetch(variant: str) -> Path:
        if variant not in REAL_LEROBOT_FIXTURES:
            raise ValueError(f"unknown LeRobot fixture variant {variant!r}")
        repo, files = REAL_LEROBOT_FIXTURES[variant]
        root = REAL_DATA_ROOT / repo.split("/")[-1]
        for relative in files:
            target = root / relative
            if target.exists() and target.stat().st_size > 0:
                continue
            try:
                _download(HUB.format(repo=repo, path=relative), target)
            except FixtureUnavailable as exc:
                pytest.skip(f"real-data fixture unavailable ({exc})")
        return root

    return fetch


@pytest.fixture(scope="session")
def mcap_log() -> Any:
    """`scripts/make_mcap_log.py`, loaded once for the session."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "make_mcap_log.py"
    spec = importlib.util.spec_from_file_location("_make_mcap_log", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
