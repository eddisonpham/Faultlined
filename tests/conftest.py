"""Shared test helpers.

PostgreSQL-backed tests run against a dedicated `<name>_test` database rather than
the development one. They share a job queue, and a worker from `just run` would
otherwise claim the jobs a test submitted, making the suite flaky whenever the app
happens to be running.

Real-format fixtures are downloaded from the Hugging Face Hub on first use into the
gitignored `var/real-data/`, and the download is a plain per-file HTTPS GET rather than
the `huggingface_hub` SDK (see the 2026-09-29 re-verification pass in
`agents/research/technology-matrix.md`). Only the tabular slice of each dataset is
fetched; camera MP4 shards are hundreds of megabytes and are not part of a read test.
Tests that need them are marked `network` and skip cleanly when offline, so the suite
still runs on a plane.
"""

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
"""The Hub's CDN resets connections from non-browser agents for some repositories.

`python-urllib/3.14` and `python-requests` both get `[WinError 10054]` on
`yaak-ai/lerobot-driving-school`, while a `Mozilla/5.0 (compatible; ...)` agent
succeeds. The `compatible;` form is the standard way to name a real client behind a
browser token, and the project URL is in there so the identification is honest.
"""

# Two genuinely different on-disk layouts, both real Hub datasets. The v2 fixture is
# an odd embodiment on purpose: it proves the reader keys off `codebase_version` and
# the declared features, not off a hard-coded SO-101 schema.
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
    """DSN for the test database, or empty when no database is configured.

    Not named `test_*`: test modules import it, and pytest would collect the
    helper itself as a test.
    """
    base = os.environ.get("DE_DATABASE_URL")
    if not base:
        return ""
    info = conninfo_to_dict(base)
    dbname = info.get("dbname") or "data_engine"
    test_dbname = f"{dbname}{TEST_DB_SUFFIX}"
    _ensure_database(info, test_dbname)
    return make_conninfo(**{**info, "dbname": test_dbname})


def pin_a_healthy_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the host sample the monitor reads, for tests that assert on signals.

    The resource detector reads *this machine*: `memory_used_ratio > 0.92` opens
    a real `RESOURCE_DEGRADED` signal, and a development box running the suite
    sits within a percentage point of that floor. A test that asserts a signal
    set then passes or fails with whatever else is open, which is a test of the
    laptop rather than of the software. Pin the input and the assertion means
    what it says.

    Only for tests where the host is an input rather than the subject: the
    detector test that exercises `RESOURCE_DEGRADED` on purpose builds its own
    feature vector and does not come through here.
    """
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
    """A real-data fixture could not be fetched intact.

    A separate exception rather than a bare ``pytest.skip`` inside the fetcher, so
    the retry logic is testable without a pytest outcome escaping through it.
    """


def _download(url: str, target: Path) -> None:
    """Fetch one file, retrying transient resets. Raises if it cannot be had whole.

    Truncation is the failure this guards hardest. A CDN that drops the
    connection mid-body raises ``IncompleteRead`` - an ``http.client`` exception,
    not an ``OSError`` - so it used to escape the retry entirely and error the
    run. A body that arrives short *without* an error used to be worse: it was
    written as if complete, and the failure surfaced three layers down as a
    Parquet error or, worse, as a plausible-looking wrong row count. Both now
    fail the attempt and retry, because a fixture that is not the fixture is
    worse than no fixture.
    """
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
                    # The body stopped early. Reported as an OSError so every
                    # way of arriving short - reset mid-transfer, declared length
                    # not met - retries and reports the same reason.
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
                # Skipped, never failed: the Hub being unreachable or serving a
                # truncated body is a fact about the network, not about the code
                # under test. A red build that says "Hugging Face was rude" sends
                # the reader to the wrong place entirely.
                pytest.skip(f"real-data fixture unavailable ({exc})")
        return root

    return fetch


@pytest.fixture(scope="session")
def mcap_log() -> Any:
    """`scripts/make_mcap_log.py`, loaded once for the session.

    The same generator backs the reader's unit tests and the B-002 benchmark
    workload. A test that regenerated the log differently from the benchmark would be
    measuring a different format than the one it claims, so there is exactly one
    entry point and it is the committed script.
    """
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "make_mcap_log.py"
    spec = importlib.util.spec_from_file_location("_make_mcap_log", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
