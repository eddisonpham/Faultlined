"""Metric primitives shared by runtime instrumentation and benchmark results."""

from __future__ import annotations

import json
import logging
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


logger = logging.getLogger(__name__)


class RuntimeMetrics:
    """Emit the observability registry's runtime signals; never fails the caller.

    Telemetry absence must not break the pipeline (ADR 0008), so sink write errors are
    logged once per failure and swallowed. Without a sink the recorder is a no-op.
    """

    def __init__(self, sink: JsonlMetricSink | None = None) -> None:
        self.sink = sink

    def _emit(
        self, name: str, value: float, unit: str, labels: dict[str, str] | None = None
    ) -> None:
        if self.sink is None:
            return
        try:
            self.sink.write(metric_point(name, value, unit, labels=labels))
        except OSError as exc:
            logger.warning(
                "metric write failed",
                extra={"event": "metric_write_failed", "error_type": type(exc).__name__},
            )

    def queue_depth(self, depth: int, *, state: str = "queued") -> None:
        self._emit("jobs_queue_depth", depth, "jobs", {"state": state})

    def job_queue_time(self, seconds: float, *, job_type: str) -> None:
        self._emit("jobs_queue_time_seconds", seconds, "seconds", {"job_type": job_type})

    def job_run_time(self, seconds: float, *, job_type: str, state: str) -> None:
        self._emit(
            "jobs_run_time_seconds", seconds, "seconds", {"job_type": job_type, "state": state}
        )

    def job_failure(self, *, job_type: str, reason_code: str) -> None:
        self._emit(
            "jobs_failures_total", 1, "count", {"job_type": job_type, "reason_code": reason_code}
        )

    def job_retry(self, *, job_type: str, attempt: int) -> None:
        self._emit(
            "jobs_retries_total", 1, "count", {"job_type": job_type, "attempt": str(attempt)}
        )

    def job_cancel(self, *, job_type: str) -> None:
        self._emit("jobs_cancellations_total", 1, "count", {"job_type": job_type})

    def job_timeout(self, *, job_type: str) -> None:
        self._emit("jobs_timeouts_total", 1, "count", {"job_type": job_type})

    def stage_duration(self, seconds: float, *, stage: str, status: str) -> None:
        self._emit(
            "pipeline_stage_duration_seconds",
            seconds,
            "seconds",
            {"stage": stage, "status": status},
        )

    def episode_ingested(self, *, episode_format: str, status: str) -> None:
        self._emit(
            "episodes_ingested_total", 1, "episodes", {"format": episode_format, "status": status}
        )

    def artifact_written(self, size_bytes: int, *, kind: str = "blob") -> None:
        self._emit("artifacts_written_bytes_total", size_bytes, "bytes", {"kind": kind})
