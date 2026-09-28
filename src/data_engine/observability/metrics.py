"""Metric primitives shared by runtime instrumentation and benchmark results."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class MetricPoint:
    name: str
    value: float
    unit: str
    labels: dict[str, str]
    timestamp: str
    source: str
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Timer:
    """Monotonic elapsed-time timer in seconds."""

    def __enter__(self) -> Timer:
        self._started = time.perf_counter()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.elapsed_seconds = time.perf_counter() - self._started


def metric_point(
    name: str,
    value: float,
    unit: str,
    *,
    labels: dict[str, str] | None = None,
    source: str = "runtime",
    correlation_id: str | None = None,
) -> MetricPoint:
    if not name or any(char.isupper() for char in name):
        raise ValueError("metric name must be non-empty lowercase snake_case")
    if any(key in {"job_id", "episode_id", "correlation_id"} for key in (labels or {})):
        raise ValueError("metric labels must not contain high-cardinality identifiers")
    return MetricPoint(
        name=name,
        value=float(value),
        unit=unit,
        labels=dict(labels or {}),
        timestamp=datetime.now(UTC).isoformat(),
        source=source,
        correlation_id=correlation_id,
    )


class JsonlMetricSink:
    """Append metric records to a local JSONL file (runtime data is gitignored)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, point: MetricPoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(point.to_dict(), sort_keys=True, separators=(",", ":")))
            stream.write("\n")
