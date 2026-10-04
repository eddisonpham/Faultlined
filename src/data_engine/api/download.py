"""Streamed CSV/JSONL downloads of the existing list reads (ADR 0030).

An export is the same read with a different serializer: rows are pulled from the
catalog cursor one page at a time and serialized as they go, so no download ever
materializes the full result set in memory. There is no export service and no
materialized export artifact; the CSV a table downloads is byte-for-byte the
view it was rendered from, and its header is the response model's field order,
so a CSV cannot drift from the JSON contract.

The first row is pulled eagerly so catalog failures surface as a normal JSON
error instead of a half-written file; everything after it streams.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from io import StringIO
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

#: Supported download serializations. A CSV is a contract too.
DOWNLOAD_FORMATS = ("csv", "jsonl")

#: One catalog round trip per page.
PAGE_SIZE = 500

#: Hard ceiling on a single download. A streaming export that can never end is
#: an outage with a progress bar; the ceiling is generous (a 500-wide page table
#: shows 50) and the JSON view's cursor remains available for more.
MAX_EXPORT_ROWS = 50_000

#: An upper cursor far in the future. A read whose default ordering is not
#: cursor-stable (the episode flag rankings reorder by signal) pages
#: consistently when every page - including the first - goes through the cursor
#: path, so such a read starts its download from here.
CURSOR_START = datetime(9999, 12, 31, tzinfo=UTC)

Fetch = Callable[..., list[dict[str, Any]]]


def parse_download_format(value: str | None) -> str | None:
    """Validate ``?format=``; unknown values are a 422 like every other filter."""
    if value is None:
        return None
    if value not in DOWNLOAD_FORMATS:
        raise HTTPException(status_code=422, detail=f"unknown download format: {value}")
    return value


def columns_of(model: type[BaseModel]) -> tuple[str, ...]:
    """The model's field order - the stable column order of every download."""
    return tuple(model.model_fields)


def _jsonable(value: Any) -> Any:
    """Value as something ``json.dumps`` accepts, without changing its meaning."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _cell(value: Any) -> str:
    """One CSV cell. Structured values become their JSON form, kept in one cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


def _csv_line(values: Sequence[str]) -> str:
    buffer = StringIO()
    csv.writer(buffer, lineterminator="\n").writerow(values)
    return buffer.getvalue()


def paged_rows(fetch: Fetch) -> Iterator[dict[str, Any]]:
    """Walk a newest-first cursor read to exhaustion, one page at a time.

    The cursor is the page's last ``created_at`` - the field every list read
    pages on. A read without one exports its first page and stops rather than
    looping on a cursor that never moves.
    """
    before: datetime | None = None
    emitted = 0
    while emitted < MAX_EXPORT_ROWS:
        batch = fetch(limit=PAGE_SIZE, before=before)
        if not batch:
            return
        for row in batch:
            yield row
            emitted += 1
            if emitted >= MAX_EXPORT_ROWS:
                return
        if len(batch) < PAGE_SIZE:
            return
        last = batch[-1].get("created_at")
        if not isinstance(last, datetime):
            return
        before = last


def download_response(
    *,
    stem: str,
    fmt: str,
    columns: Sequence[str],
    rows: Iterable[dict[str, Any]],
    item_model: type[BaseModel] | None = None,
) -> StreamingResponse:
    """Serialize rows as ``fmt`` and stream them as a file download.

    ``item_model`` normalizes each raw catalog row through the same Pydantic
    model the JSON route validates with, so a CSV row and a JSON item are the
    same values in the same shapes - what you see is what you get.
    """
    filename = f"{stem}-{datetime.now(UTC).date().isoformat()}.{'csv' if fmt == 'csv' else 'jsonl'}"
    media_type = (
        "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson; charset=utf-8"
    )

    def normalized() -> Iterator[dict[str, Any]]:
        for raw in rows:
            if item_model is None:
                yield raw
            else:
                yield item_model.model_validate(raw).model_dump(mode="json")

    def rendered(row: dict[str, Any]) -> str:
        if fmt == "csv":
            return _csv_line([_cell(row.get(column)) for column in columns])
        return (
            json.dumps(
                {column: _jsonable(row.get(column)) for column in columns},
                ensure_ascii=False,
            )
            + "\n"
        )

    iterator = iter(normalized())
    # Pulled before the response starts streaming, so a catalog failure raises
    # here (a normal JSON error) rather than halfway through a partial file.
    first = next(iterator, None)

    def lines() -> Iterator[str]:
        if fmt == "csv":
            yield _csv_line(columns)
        if first is not None:
            yield rendered(first)
        for row in iterator:
            yield rendered(row)

    return StreamingResponse(
        lines(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
