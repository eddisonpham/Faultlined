"""Contract tests for ``?format=csv|jsonl`` downloads on the list routes (ADR 0030)."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.api import download
from data_engine.api.app import create_app
from data_engine.api.schemas import (
    ArtifactSummary,
    ContractPayload,
    EpisodeSummary,
    FailingEpisodeRow,
    IncidentPayload,
    JobSummary,
)
from data_engine.config import Settings

STAMP = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _job(n: int) -> dict[str, Any]:
    return {
        "id": f"job-{n}",
        "type": "ingest",
        "state": "succeeded",
        "correlation_id": f"corr-{n}",
        "error": None,
        "attempts": 1,
        "max_attempts": 3,
        "created_at": STAMP - timedelta(minutes=n),
        "started_at": STAMP,
        "finished_at": STAMP,
    }


def _episode(n: int) -> dict[str, Any]:
    return {
        "id": f"ep-{n}",
        "episode_key": f"key-{n}",
        "source_hash": "abc",
        "artifact_hash": "def",
        "format": "lerobot",
        "state": "validated",
        "created_at": STAMP - timedelta(minutes=n),
        "frame_count": 10,
    }


class FakeCatalog:
    """Rows shaped like the real catalog returns them (real datetimes), paging for real."""

    def __init__(self) -> None:
        self.before_args: list[Any] = []
        self.jobs = [_job(1), _job(2)]
        self.episodes = [_episode(1), _episode(2)]

    @staticmethod
    def _page(rows: list[dict[str, Any]], limit: int, before: Any) -> list[dict[str, Any]]:
        candidates = [r for r in rows if before is None or r["created_at"] < before]
        return candidates[:limit]

    def list_jobs(
        self, *, state: Any = None, job_type: Any = None, limit: int = 50, before: Any = None
    ) -> list[dict[str, Any]]:
        return self._page(self.jobs, limit, before)

    def list_artifacts(self, *, limit: int = 50, before: Any = None) -> list[dict[str, Any]]:
        rows = [
            {"hash": "a" * 64, "size_bytes": 12, "created_at": STAMP, "episode_ids": ["ep-1"]},
            {
                "hash": "b" * 64,
                "size_bytes": 34,
                "created_at": STAMP - timedelta(minutes=1),
                "episode_ids": [],
            },
        ]
        return self._page(rows, limit, before)

    def list_episodes(
        self, *, limit: int = 50, state: Any = None, flag: Any = None, before: Any = None
    ) -> list[dict[str, Any]]:
        self.before_args.append(before)
        return self._page(self.episodes, limit, before)

    def failing_episodes(
        self, *, limit: int = 50, before: Any = None, reason_code: Any = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "ep-9",
                "episode_key": "key-9",
                "format": "mcap",
                "state": "quarantined",
                "created_at": STAMP,
                "profile_name": "strict",
                "profile_version": "1",
                "reason_codes": ["NON_FINITE"],
                "violations": [{"rule": "finite"}],
            }
        ]

    def list_incidents(
        self,
        *,
        severity: Any = None,
        status: Any = None,
        label: Any = None,
        limit: int = 50,
        before: Any = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "inc-1",
                "fingerprint": "f",
                "label": "queue_depth",
                "severity": "high",
                "summary": "queue is deep",
                "evidence": {"queue_depth": 12},
                "status": "open",
                "first_seen": STAMP,
                "last_seen": STAMP,
            }
        ]

    def list_contracts(
        self, *, outcome: Any = None, limit: int = 50, before: Any = None
    ) -> list[dict[str, Any]]:
        return [{"job_id": "job-1", "outcome": "met", "created_at": STAMP, "updated_at": STAMP}]

    def count_jobs_by_state(self) -> dict[str, int]:
        return {"queued": 0, "running": 0, "succeeded": len(self.jobs)}


@pytest.fixture
def metrics_file(tmp_path: Path) -> Path:
    return tmp_path / "runtime.jsonl"


def _client(catalog: FakeCatalog | None = None, metrics_file: Path | None = None) -> TestClient:
    app = create_app(
        Settings(_env_file=None, metrics_path=metrics_file or Path("var/metrics/runtime.jsonl")),
        initialize_database=False,
        catalog=catalog or FakeCatalog(),  # type: ignore[arg-type]
    )
    return TestClient(app)


@pytest.mark.contract
class TestDownloadContract:
    @pytest.mark.parametrize(
        ("path", "model"),
        [
            ("/api/v1/jobs", JobSummary),
            ("/api/v1/artifacts", ArtifactSummary),
            ("/api/v1/episodes", EpisodeSummary),
            ("/api/v1/failures/episodes", FailingEpisodeRow),
            ("/api/v1/incidents", IncidentPayload),
            ("/api/v1/contracts", ContractPayload),
        ],
    )
    def test_csv_header_is_the_response_model_field_order(
        self, path: str, model: Any, metrics_file: Path
    ) -> None:
        response = _client(metrics_file=metrics_file).get(path, params={"format": "csv"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        header = response.text.splitlines()[0]
        assert header == ",".join(model.model_fields)

    def test_csv_rows_match_the_json_view(self, metrics_file: Path) -> None:
        client = _client(metrics_file=metrics_file)
        items = client.get("/api/v1/jobs").json()["items"]

        response = client.get("/api/v1/jobs", params={"format": "csv"})
        rows = list(csv.reader(StringIO(response.text)))
        header, data = rows[0], rows[1:]

        assert len(data) == len(items)
        assert [dict(zip(header, row, strict=True))["id"] for row in data] == [
            i["id"] for i in items
        ]

    def test_jsonl_rows_parse_back_to_the_json_view(self, metrics_file: Path) -> None:
        client = _client(metrics_file=metrics_file)
        items = client.get("/api/v1/episodes").json()["items"]

        response = client.get("/api/v1/episodes", params={"format": "jsonl"})
        assert response.headers["content-type"].startswith("application/x-ndjson")
        rows = [json.loads(line) for line in response.text.splitlines()]

        assert [row["id"] for row in rows] == [i["id"] for i in items]
        assert rows[0]["created_at"] == items[0]["created_at"]

    def test_an_unknown_format_is_a_422(self, metrics_file: Path) -> None:
        response = _client(metrics_file=metrics_file).get("/api/v1/jobs", params={"format": "xlsx"})

        assert response.status_code == 422
        assert "xlsx" in response.text

    def test_the_filename_names_the_table_and_the_date(self, metrics_file: Path) -> None:
        response = _client(metrics_file=metrics_file).get(
            "/api/v1/artifacts", params={"format": "csv"}
        )

        disposition = response.headers["content-disposition"]
        assert 'filename="artifacts-' in disposition
        assert disposition.endswith('.csv"')

    def test_downloads_reach_the_catalog_through_the_cursor_path(self, metrics_file: Path) -> None:
        """The episode download pages on created_at even though the view ranks by flag."""
        catalog = FakeCatalog()
        _client(catalog, metrics_file=metrics_file).get(
            "/api/v1/episodes", params={"format": "csv", "flag": "jerky"}
        )

        assert catalog.before_args == [download.CURSOR_START]

    def test_a_paged_download_follows_the_cursor_to_the_end(
        self, monkeypatch: pytest.MonkeyPatch, metrics_file: Path
    ) -> None:
        monkeypatch.setattr(download, "PAGE_SIZE", 1)
        catalog = FakeCatalog()
        response = _client(catalog, metrics_file=metrics_file).get(
            "/api/v1/jobs", params={"format": "jsonl"}
        )

        rows = [json.loads(line) for line in response.text.splitlines()]
        assert [row["id"] for row in rows] == ["job-1", "job-2"]

    @pytest.mark.parametrize(
        ("page", "api"),
        [
            ("/ui/jobs", "/api/v1/jobs"),
            ("/ui/artifacts", "/api/v1/artifacts"),
            ("/ui/episodes", "/api/v1/episodes"),
            ("/ui/failures", "/api/v1/failures/episodes"),
            ("/ui/incidents", "/api/v1/incidents"),
            ("/ui/metrics", "/api/v1/metrics"),
        ],
    )
    def test_a_ui_download_link_resolves_to_the_shared_serializer(
        self, page: str, api: str, metrics_file: Path
    ) -> None:
        """The link is the page's own URL; the route hands the browser the file."""
        client = _client(metrics_file=metrics_file)

        response = client.get(page, params={"format": "csv"}, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"].startswith(api + "?")

        response = client.get(page, params={"format": "csv"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")

    def test_the_ui_page_still_never_shows_the_transport(self, metrics_file: Path) -> None:
        html = _client(metrics_file=metrics_file).get("/ui/jobs").text
        body = html.split("<main", 1)[-1]
        assert "/api/v1" not in body
        assert 'href="/ui/jobs?format=csv"' in body

    def test_metrics_downloads_the_series_for_the_window(self, metrics_file: Path) -> None:
        records = [
            {
                "name": "jobs_queue_depth",
                "value": 1.0,
                "timestamp": STAMP.isoformat(),
                "labels": {},
            },
            {
                "name": "jobs_queue_depth",
                "value": 3.0,
                "timestamp": STAMP.isoformat(),
                "labels": {},
            },
        ]
        metrics_file.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

        response = _client(metrics_file=metrics_file).get(
            "/api/v1/metrics", params={"format": "csv"}
        )

        assert response.status_code == 200
        rows = list(csv.reader(StringIO(response.text)))
        assert rows[0] == ["metric", "t", "v", "count"]
        assert rows[1][0] == "jobs_queue_depth"
        assert rows[1][2] == "2.0"
