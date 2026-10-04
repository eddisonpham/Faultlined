"""Unit tests for the streamed download serializer (ADR 0030)."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from io import StringIO
from typing import Any

import anyio
import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from data_engine.api import download
from data_engine.api.download import (
    columns_of,
    download_response,
    paged_rows,
    parse_download_format,
)
from data_engine.api.schemas import JobSummary


class TinyRow(BaseModel):
    id: str
    count: int = 0
    note: str | None = None


class TestParseDownloadFormat:
    def test_absent_means_the_json_view(self) -> None:
        assert parse_download_format(None) is None

    @pytest.mark.parametrize("fmt", ["csv", "jsonl"])
    def test_supported_formats_pass(self, fmt: str) -> None:
        assert parse_download_format(fmt) == fmt

    def test_an_unknown_format_is_a_422_like_any_other_filter(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            parse_download_format("xlsx")
        assert excinfo.value.status_code == 422
        assert "xlsx" in str(excinfo.value.detail)


class TestColumns:
    def test_columns_are_the_model_field_order(self) -> None:
        assert columns_of(JobSummary) == (
            "id",
            "type",
            "state",
            "correlation_id",
            "error",
            "attempts",
            "max_attempts",
            "created_at",
            "started_at",
            "finished_at",
        )


class TestPagedRows:
    def test_pages_until_a_short_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(download, "PAGE_SIZE", 1)
        # Newest first, like every list read the pager walks.
        stamps = [datetime(2026, 10, 1, hour=h, tzinfo=UTC) for h in (3, 2, 1)]
        calls: list[dict[str, Any]] = []

        def fetch(*, limit: int, before: datetime | None) -> list[dict[str, Any]]:
            calls.append({"limit": limit, "before": before})
            batch = [
                {"id": f"row-{h}", "created_at": stamp}
                for h, stamp in enumerate(stamps)
                if before is None or stamp < before
            ]
            return batch[:limit]

        rows = list(paged_rows(fetch))

        assert [row["id"] for row in rows] == ["row-0", "row-1", "row-2"]
        assert [call["before"] for call in calls] == [None, stamps[0], stamps[1], stamps[2]]

    def test_a_read_without_a_cursor_exports_one_page(self) -> None:
        def fetch(*, limit: int, before: datetime | None) -> list[dict[str, Any]]:
            return [{"id": "row", "created_at": None}] * limit

        assert len(list(paged_rows(fetch))) == download.PAGE_SIZE

    def test_the_row_ceiling_stops_a_runaway_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(download, "MAX_EXPORT_ROWS", 3)
        monkeypatch.setattr(download, "PAGE_SIZE", 2)

        def fetch(*, limit: int, before: datetime | None) -> list[dict[str, Any]]:
            stamp = before or datetime(2026, 10, 1, tzinfo=UTC)
            return [{"id": "row", "created_at": stamp}] * limit

        assert len(list(paged_rows(fetch))) == 3


def _drain(response: StreamingResponse) -> str:
    """Consume a streaming response body, sync iterators included."""

    async def collect() -> str:
        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
        return b"".join(chunks).decode()

    return anyio.run(collect)


class TestDownloadResponse:
    def test_csv_leads_with_the_stable_header(self) -> None:
        rows = [{"id": "a", "count": 2, "note": None, "extra": "dropped"}]
        response = download_response(
            stem="things",
            fmt="csv",
            columns=columns_of(TinyRow),
            rows=iter(rows),
            item_model=TinyRow,
        )
        lines = _drain(response).strip().split("\n")

        assert lines[0] == "id,count,note"
        assert lines[1] == "a,2,"

    def test_jsonl_rows_are_the_model_values(self) -> None:
        rows = [{"id": "a", "count": 2}]
        response = download_response(
            stem="things",
            fmt="jsonl",
            columns=columns_of(TinyRow),
            rows=iter(rows),
            item_model=TinyRow,
        )

        assert json.loads(_drain(response)) == {"id": "a", "count": 2, "note": None}

    def test_structured_cells_stay_in_one_csv_cell(self) -> None:
        response = download_response(
            stem="things",
            fmt="csv",
            columns=("id", "tags"),
            rows=iter([{"id": "a", "tags": ["x, y", "z"]}]),
        )
        parsed = list(csv.reader(StringIO(_drain(response).strip())))

        assert parsed[1][1] == '["x, y", "z"]'

    def test_the_filename_and_media_type_name_the_format(self) -> None:
        for fmt, suffix, media in (
            ("csv", ".csv", "text/csv"),
            ("jsonl", ".jsonl", "application/x-ndjson"),
        ):
            response = download_response(stem="jobs", fmt=fmt, columns=("id",), rows=iter([]))
            disposition = response.headers["content-disposition"]
            assert 'filename="jobs-' in disposition and disposition.endswith(suffix + '"')
            assert media in response.media_type

    def test_an_empty_result_is_still_a_valid_file(self) -> None:
        response = download_response(stem="things", fmt="csv", columns=("id",), rows=iter([]))

        assert _drain(response) == "id\n"

    def test_a_failing_read_raises_before_the_response_exists(self) -> None:
        def rows() -> Any:
            raise RuntimeError("catalog down")
            yield {}

        with pytest.raises(RuntimeError, match="catalog down"):
            download_response(stem="things", fmt="csv", columns=("id",), rows=rows())
