"""Chaos harness (B-016): the monitor evaluated against labeled injected faults."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from data_engine.monitoring.features import ResourceSample
from data_engine.monitoring.service import MonitorService
from data_engine.monitoring.signals import Label

HEARTBEAT_INTERVAL_SECONDS = 5.0

REPLAY_HOST = ResourceSample(
    cpu_percent=11.0,
    memory_used_bytes=8 * 1024**3,
    memory_available_bytes=24 * 1024**3,
    disk_free_bytes=500 * 1024**3,
)


def _metric_record(
    name: str,
    value: float,
    *,
    at: datetime,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One telemetry record in the shape `read_metric_records` returns."""
    return {
        "name": name,
        "value": value,
        "labels": dict(labels or {}),
        "timestamp": at.isoformat(),
        "source": "chaos",
    }


@dataclass(frozen=True, slots=True)
class FaultWindow:
    """One labeled fault over one tick window."""

    label: str
    """The expected detector label (e.g. """

    onset: datetime
    """When the fault starts in replay time."""

    detectable_at: datetime
    """onset + aggregation window; the honest lower bound for detection."""

    records: list[dict[str, Any]] = field(default_factory=list)
    """Telemetry for this window, real records shaped like the sink's."""

    snapshot: dict[str, Any] | None = None
    """Injected queue/catalog snapshot, or None to leave the service's probe alone."""


def heartbeat_records(
    *,
    count: int,
    end_at: datetime,
    interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> list[dict[str, Any]]:
    """A healthy heartbeat history ending at ``end_at``."""
    return [
        _metric_record(
            "workers_heartbeat_age_seconds",
            0.0,
            at=end_at - timedelta(seconds=i * interval_seconds),
        )
        for i in range(count)
    ]


def worker_lost_window(
    *, onset: datetime, window_seconds: float, label: str = Label.WORKER_LOST.value
) -> FaultWindow:
    """Heartbeats stop for real while the queue holds work (plan §6.2)."""
    stale_for = max(window_seconds, 60.0) * 2 + 60.0
    return FaultWindow(
        label=label,
        onset=onset,
        detectable_at=onset + timedelta(seconds=window_seconds),
        records=heartbeat_records(count=1, end_at=onset - timedelta(seconds=stale_for)),
        snapshot={
            "queue_depth": {"queued": 2, "running": 1},
            "oldest_queued_age_seconds": 30.0,
        },
    )


def queue_backlog_window(
    *, onset: datetime, window_seconds: float, burst: int = 40, label: str = ""
) -> FaultWindow:
    """A burst larger than the single worker can drain (plan §6.2)."""
    return FaultWindow(
        label=label,
        onset=onset,
        detectable_at=onset + timedelta(seconds=window_seconds),
        records=heartbeat_records(count=3, end_at=onset),
        snapshot={
            "queue_depth": {"queued": burst, "running": 1},
            "oldest_queued_age_seconds": float(burst) * 2.0,
        },
    )


def clean_window(*, onset: datetime, window_seconds: float, warm: bool = True) -> FaultWindow:
    """A null window: healthy heartbeats, a moving queue, nothing wrong."""
    records = heartbeat_records(count=3, end_at=onset)
    if warm:
        records.append(
            _metric_record("episodes_ingested_total", 1.0, at=onset - timedelta(seconds=1))
        )
    return FaultWindow(
        label="",
        onset=onset,
        detectable_at=onset + timedelta(seconds=window_seconds),
        records=records,
        snapshot={
            "queue_depth": {"queued": 1, "running": 0},
            "oldest_queued_age_seconds": 1.0,
        },
    )


class ChaosMonitor(MonitorService):
    """The production monitor with three replay seams and no other overrides."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._chaos_records: list[dict[str, Any]] = []
        self._chaos_snapshot: dict[str, Any] | None = None

    def inject(self, window: FaultWindow) -> None:
        """Arm the seams for the next tick; call once per window."""
        self._chaos_records = list(window.records)
        self._chaos_snapshot = window.snapshot

    def _records(self, reference: datetime) -> list[dict[str, Any]]:  # noqa: ARG002
        return list(self._chaos_records)

    def _probe(self, reference: datetime, catalog: Any) -> tuple[dict[str, Any], bool]:
        if self._chaos_snapshot is not None:
            return dict(self._chaos_snapshot), True
        return super()._probe(reference, catalog)

    def _resources(self) -> ResourceSample | None:
        return REPLAY_HOST


@dataclass(frozen=True, slots=True)
class WindowOutcome:
    """What the monitor did with one window."""

    label: str
    fired_labels: list[str]
    detected: bool
    latency_seconds: float | None
    incidents_touched: int
    suppressed: dict[str, int]
    budget_exhausted: bool
    blind: bool
    warm_scopes: int
    observed_at: datetime


def evaluate_window(
    monitor: ChaosMonitor,
    window: FaultWindow,
    *,
    tick_at: datetime | None = None,
) -> WindowOutcome:
    """Run one tick against an armed window and score it."""
    monitor.inject(window)
    reference = tick_at or window.detectable_at
    monitor._now = reference
    report = monitor.tick()
    labels = sorted({signal.label for signal in report.signals})
    detected = bool(window.label) and window.label in labels
    latency: float | None = None
    if detected:
        latency = (reference - window.detectable_at).total_seconds()
    return WindowOutcome(
        label=window.label,
        fired_labels=labels,
        detected=detected,
        latency_seconds=latency,
        incidents_touched=len(report.written),
        suppressed=dict(report.suppressed),
        budget_exhausted=report.budget_exhausted,
        blind=report.blind,
        warm_scopes=report.warm_scopes,
        observed_at=report.observed_at,
    )


def evaluate_sequence(monitor: ChaosMonitor, windows: list[FaultWindow]) -> list[WindowOutcome]:
    """Run windows back to back on one monitor, in replay order."""
    outcomes: list[WindowOutcome] = []
    previous: datetime | None = None
    for window in windows:
        if previous is not None and window.detectable_at < previous:
            raise ValueError("windows must be ordered by detectable_at")
        previous = window.detectable_at
        outcomes.append(evaluate_window(monitor, window))
    return outcomes


__all__ = [
    "REPLAY_HOST",
    "ChaosMonitor",
    "FaultWindow",
    "WindowOutcome",
    "clean_window",
    "evaluate_sequence",
    "evaluate_window",
    "heartbeat_records",
    "queue_backlog_window",
    "worker_lost_window",
]
