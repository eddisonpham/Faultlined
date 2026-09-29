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


def read_metric_records(path: Path, *, max_records: int | None = None) -> list[dict[str, Any]]:
    """Read metric records from a JSONL sink.

    A missing file is empty telemetry, not an error. Malformed lines are skipped
    rather than failing the reader: a truncated tail line after a crash must not
    hide every healthy record before it. ``max_records`` keeps only the newest N.
    """
    if not path.exists():
        return []
    records: deque[dict[str, Any]] = deque(maxlen=max_records)
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "name" in record and "value" in record:
                records.append(record)
    return list(records)


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


def heartbeat_age_seconds(
    records: Iterable[dict[str, Any]], *, now: datetime | None = None
) -> float | None:
    """Seconds since the newest worker heartbeat record, or None if never seen.

    The heartbeat record carries value 0 ("age at emission"); its timestamp is the
    heartbeat. Age is therefore derived at read time instead of being emitted as a
    stale gauge.
    """
    newest: datetime | None = None
    for record in records:
        if record.get("name") != WORKER_HEARTBEAT_METRIC:
            continue
        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is not None and (newest is None or timestamp > newest):
            newest = timestamp
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
