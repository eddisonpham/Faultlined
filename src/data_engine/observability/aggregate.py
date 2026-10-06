"""Summarize JSONL metric records for dashboards and the metrics API.

The JSONL sink remains the source of truth (ADR 0017); this module is pure
functions over already-emitted records so it is testable without a process.
Percentiles use the nearest-rank method: the value at ``ceil(q * n) - 1`` of the
sorted samples.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORKER_HEARTBEAT_METRIC = "workers_heartbeat_age_seconds"
#: Emitted once per monitor tick (ADR 0020). A tick runs in whichever process
#: schedules it, so these two records are how a *different* process learns when the
#: monitor last ran and whether it was blind (ADR 0031).
MONITOR_TICK_METRIC = "monitor_tick_seconds"
MONITOR_BLIND_METRIC = "monitor_blind"

#: Backward chunk size for a newest-N read. One MiB keeps a tick's read a few
#: pages regardless of how long the sink has grown; 64 MiB is the ceiling past
#: which we stop hunting for the window rather than read a pathological file.
TAIL_CHUNK_BYTES = 1 << 20
TAIL_MAX_BYTES = 64 << 20


def _parse_line(line: bytes) -> dict[str, Any] | None:
    """One JSONL line to a record, or None when blank, corrupt, or not a metric."""
    text = line.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(record, dict) and "name" in record and "value" in record:
        return record
    return None


def read_metric_records(path: Path, *, max_records: int | None = None) -> list[dict[str, Any]]:
    """Read metric records from a JSONL sink, newest N when a limit is given.

    A missing file is empty telemetry, not an error. Malformed lines are skipped
    rather than failing the reader: a truncated tail line after a crash must not
    hide every healthy record before it.

    With ``max_records`` the file is read **from the end** in chunks and stops as
    soon as N valid records are in hand, so a read costs the requested window,
    not the history. That is load-bearing: the monitor re-reads the sink every
    tick and the API reads it on every request, and EXP-0007 measured the
    full-history parse at half a minute's worth of a 500k-record sink. Callers
    that genuinely want the whole file still pass ``max_records=None``.
    """
    if not path.exists():
        return []
    if max_records is not None and max_records <= 0:
        return []
    if max_records is not None:
        return _read_tail(path, max_records)
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            record = _parse_line(line.encode("utf-8"))
            if record is not None:
                records.append(record)
    return records


def _read_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    """The newest ``limit`` valid records, read backward, chronological on return.

    Walks the file from the end in ``TAIL_CHUNK_BYTES`` blocks so the cost is the
    window's, not the file's. A chunk boundary can split a line: the fragment is
    carried to the next (earlier) block, and the file's first line is completed
    after the loop. Corrupt lines inside the window are skipped exactly as the
    forward read skips them. The walk stops early at ``TAIL_MAX_BYTES`` - a sink
    whose newest 64 MiB holds no valid record is not worth paging through.
    """
    newest_first: deque[dict[str, Any]] = deque(maxlen=limit)
    size = path.stat().st_size
    position = size
    carried = b""
    with path.open("rb") as stream:
        while position > 0 and len(newest_first) < limit:
            if size - position >= TAIL_MAX_BYTES:
                break
            chunk_start = max(0, position - TAIL_CHUNK_BYTES)
            stream.seek(chunk_start)
            block = stream.read(position - chunk_start) + carried
            position = chunk_start
            lines = block.split(b"\n")
            carried = lines[0]
            for line in reversed(lines[1:]):
                record = _parse_line(line)
                if record is not None:
                    newest_first.append(record)
                    if len(newest_first) >= limit:
                        break
        if position == 0 and len(newest_first) < limit and carried.strip():
            # The walk reached the file start; ``carried`` is now the completed
            # first line. On any other exit it is a mid-file fragment.
            record = _parse_line(carried)
            if record is not None:
                newest_first.append(record)
    return list(reversed(newest_first))


def window_records(
    records: Iterable[dict[str, Any]], *, since: datetime | None
) -> list[dict[str, Any]]:
    """Keep records at or after ``since`` (parsed from each record's ISO timestamp)."""
    if since is None:
        return list(records)
    kept: list[dict[str, Any]] = []
    for record in records:
        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is not None and timestamp >= since:
            kept.append(record)
    return kept


def format_labels(labels: dict[str, str] | None) -> str:
    """Stable low-cardinality label key, e.g. ``job_type=ingest,state=succeeded``."""
    items = sorted((labels or {}).items())
    return ",".join(f"{key}={value}" for key, value in items)


def percentile(sorted_values: Sequence[float], quantile: float) -> float:
    """Nearest-rank percentile over pre-sorted values."""
    if not sorted_values:
        raise ValueError("percentile of empty sample")
    rank = max(1, math.ceil(quantile * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def summarize(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group records by (metric name, labels) and describe each sample set."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = (str(record["name"]), format_labels(record.get("labels")))
        groups[key].append(record)

    summaries: list[dict[str, Any]] = []
    for (name, label_key), group in sorted(groups.items()):
        values = sorted(float(record["value"]) for record in group)
        timestamps = [
            parsed
            for record in group
            if (parsed := _parse_timestamp(record.get("timestamp"))) is not None
        ]
        summaries.append(
            {
                "name": name,
                "labels": dict(group[-1].get("labels") or {}),
                "label_key": label_key,
                "unit": str(group[-1].get("unit") or ""),
                "count": len(values),
                "min": values[0],
                "max": values[-1],
                "mean": sum(values) / len(values),
                "sum": sum(values),
                "p50": percentile(values, 0.50),
                "p95": percentile(values, 0.95),
                "p99": percentile(values, 0.99),
                "first_timestamp": min(timestamps).isoformat() if timestamps else None,
                "last_timestamp": max(timestamps).isoformat() if timestamps else None,
            }
        )
    return summaries


def series(
    records: Iterable[dict[str, Any]], *, bucket_seconds: float = 60.0
) -> dict[str, list[dict[str, Any]]]:
    """Bucketed mean per metric name (across label sets) for sparkline rendering.

    Keys are metric names only; a sparkline is a trend, and splitting it by label
    is a job for the summaries table.
    """
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds must be positive")
    buckets: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is None:
            continue
        bucket = int(timestamp.timestamp() // bucket_seconds)
        buckets[str(record["name"])][bucket].append(float(record["value"]))

    result: dict[str, list[dict[str, Any]]] = {}
    for name, by_bucket in sorted(buckets.items()):
        points: list[dict[str, Any]] = []
        for bucket, values in sorted(by_bucket.items()):
            points.append(
                {
                    "t": datetime.fromtimestamp(bucket * bucket_seconds, tz=UTC).isoformat(),
                    "v": sum(values) / len(values),
                    "count": len(values),
                }
            )
        result[name] = points
    return result


def newest_record(records: Iterable[dict[str, Any]], *, name: str) -> dict[str, Any] | None:
    """The newest record for one metric name, or None if it was never emitted.

    The sink is the shared record between processes (ADR 0017), which is what makes
    this the honest way for one process to ask what another one is doing. A monitor
    tick runs in the worker loop (ADR 0031) while the health endpoint is served by
    the API, so reading its own memory would report `never` about a monitor that is
    running - the exact false negative the endpoint exists to prevent.
    """
    newest: dict[str, Any] | None = None
    newest_at: datetime | None = None
    for record in records:
        if record.get("name") != name:
            continue
        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is not None and (newest_at is None or timestamp > newest_at):
            newest, newest_at = record, timestamp
    return newest


def newest_metric_at(records: Iterable[dict[str, Any]], *, name: str) -> datetime | None:
    """Timestamp of the newest record for one metric name, or None."""
    record = newest_record(records, name=name)
    return _parse_timestamp(record.get("timestamp")) if record else None


def heartbeat_age_seconds(
    records: Iterable[dict[str, Any]], *, now: datetime | None = None
) -> float | None:
    """Seconds since the newest worker heartbeat record, or None if never seen.

    The heartbeat record carries value 0 ("age at emission"); its timestamp is the
    heartbeat. Age is therefore derived at read time instead of being emitted as a
    stale gauge.
    """
    newest = newest_metric_at(records, name=WORKER_HEARTBEAT_METRIC)
    if newest is None:
        return None
    reference = now or datetime.now(UTC)
    return max(0.0, (reference - newest).total_seconds())


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
