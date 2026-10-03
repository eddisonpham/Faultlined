"""Metric primitives shared by runtime instrumentation and benchmark results."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from data_engine.observability.telemetry import TelemetrySample, sample_resources


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

    def api_request(self, seconds: float, *, route: str, method: str, status_class: str) -> None:
        """Per-request latency; ``route`` is the route template, never a raw path."""
        labels = {"route": route, "method": method, "status_class": status_class}
        self._emit("api_request_duration_seconds", seconds, "seconds", labels)
        self._emit("api_requests_total", 1, "count", labels)

    def catalog_query(self, seconds: float, *, operation: str) -> None:
        self._emit("catalog_query_duration_seconds", seconds, "seconds", {"operation": operation})

    def worker_heartbeat(self, *, worker_state: str) -> None:
        """Liveness ping; the record's timestamp is the heartbeat, its value is 0."""
        self._emit("workers_heartbeat_age_seconds", 0, "seconds", {"worker_state": worker_state})

    # ---- host gauges (the `system_*` registry rows) ----

    def sample_host(self, *, process_role: str = "worker") -> None:
        """Take one host sample and record the registry's `system_*` gauges.

        This is what makes those registry rows real rather than aspirational: they
        were documented, sampled once per benchmark run into provenance, and never
        emitted on the runtime interval an operator actually watches.

        A sampling error is logged and dropped (ADR 0008) because this runs inside
        the worker loop: telemetry must not be able to stop the worker.
        """
        try:
            sample = sample_resources()
        except Exception:
            logger.warning("host sample failed", extra={"event": "host_sample_failed"})
            return
        self.emit_host_sample(sample, process_role=process_role)

    def emit_host_sample(self, sample: TelemetrySample, *, process_role: str = "worker") -> None:
        """Record one already-taken host sample.

        A field the host could not report is skipped rather than emitted as zero.
        `sample_resources` represents "could not read it" as `null`, and a fabricated
        0 would read as a measured value on the metrics page - the same reason a
        missing GPU produces no `system_gpu_*` record at all rather than a zeroed one.
        """
        gauges: tuple[tuple[str, float | int | None, str, dict[str, str] | None], ...] = (
            ("system_cpu_percent", sample.cpu_percent, "percent", None),
            ("system_memory_used_bytes", sample.memory_used_bytes, "bytes", None),
            (
                "process_rss_bytes",
                sample.process_rss_bytes,
                "bytes",
                {"process_role": process_role},
            ),
            # The sample is taken on the process's working directory, so `volume` is
            # the one directory this deployment writes artifacts under.
            ("system_disk_free_bytes", sample.disk_free_bytes, "bytes", {"volume": "."}),
            (
                "system_network_bytes_sent_total",
                sample.network_bytes_sent_total,
                "bytes",
                {"interface": "aggregate"},
            ),
            (
                "system_network_bytes_recv_total",
                sample.network_bytes_recv_total,
                "bytes",
                {"interface": "aggregate"},
            ),
            (
                "system_gpu_utilization_percent",
                sample.gpu_utilization_percent,
                "percent",
                {"device_index": "0"},
            ),
            (
                "system_gpu_memory_used_bytes",
                sample.gpu_memory_used_bytes,
                "bytes",
                {"device_index": "0"},
            ),
        )
        for name, value, unit, labels in gauges:
            if value is None:
                continue
            self._emit(name, float(value), unit, labels)

    # ---- monitor self-observability (ADR 0020) ----
    #
    # `scope` is deliberately absent from every label below. For RUN_STALLED the
    # scope is a job id, and a per-job metric series is exactly the unbounded
    # cardinality ADR 0017 forbids: it would create a time series per job, none of
    # which would ever be worth reading.

    def monitor_tick(self, seconds: float) -> None:
        self._emit("monitor_tick_seconds", seconds, "seconds")

    def monitor_signal(self, label: str, severity: object) -> None:
        self._emit("monitor_signals_total", 1, "count", {"label": label, "severity": str(severity)})

    def monitor_suppressed(self, action: str, count: int) -> None:
        self._emit("monitor_suppressed_total", count, "count", {"action": action})

    def incident_opened(self, label: str, severity: str, notify_class: str) -> None:
        self._emit("incidents_opened_total", 1, "count", {"label": label, "severity": severity})
        self._emit(
            "incidents_notified_total",
            1 if notify_class == "notify" else 0,
            "count",
            {"label": label},
        )

    def monitor_sensor_availability(self, ratio: float) -> None:
        self._emit("monitor_sensor_availability", ratio, "ratio")

    def monitor_blind(self, blind: bool) -> None:
        """1 while the monitor cannot see the platform. Never an incident: a monitor
        reports itself blind on the health endpoint rather than paging about a
        platform it is not observing."""
        self._emit("monitor_blind", 1.0 if blind else 0.0, "boolean")
