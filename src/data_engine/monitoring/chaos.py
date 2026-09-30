"""Chaos harness (B-016): the monitor evaluated against labeled injected faults.

The ground-truth rule comes from the monitoring plan (§6): **only the operator's
action space is simulated.** The feature builder, baselines, triage, and
detectors are the real implementation; an injected fault is real telemetry and
real catalog state fed through the unmodified tick pipeline. Nothing about the
feature space is invented, and no accuracy figure exists before a chaos run.

Two components:

- :class:`ChaosMonitor` - the production `MonitorService` with two narrow,
  explicitly-documented seams for replay: injected telemetry records and an
  injected queue snapshot. Everything downstream of those seams is untouched.
- :func:`evaluate_window` - one labeled window: feed records, run one tick,
  return whether the expected label fired (and what fired wrongly).

A window's ``label`` is the expected detector label, spelled exactly as the
:class:`~data_engine.monitoring.signals.Label` taxonomy spells it; it is
empty for faults no dedicated rule owns (e.g. a queue burst, whose designed
detection path is the warm-baseline residual) and for null windows.

Detection latency is measured against ``t_detectable``, not ``t_onset``: a
fault cannot be detected before the metric window containing it has closed, so
the harness's synthetic timestamps carry that delay. Measuring against onset
would report a latency the system physically cannot achieve.

Known bounded gap (plan §6.8, recorded rather than hidden): heartbeat telemetry
is replayed as records, so a `WORKER_LOST` fault is exercised as *stale*
heartbeat telemetry, not as the real process dying. The real-kill variant
belongs to the full fault-injection campaign against `just run`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from data_engine.monitoring.service import MonitorService
from data_engine.monitoring.signals import Label

#: The heartbeat cadence a healthy single-worker deployment emits.
HEARTBEAT_INTERVAL_SECONDS = 5.0


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
    """The expected detector label (e.g. ``WORKER_LOST``); ``""`` for none."""

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
    """A healthy heartbeat history ending at ``end_at``.

    Heartbeats are emitted *before* the window they close: a worker that died
    during a window still emitted its beats up to the death, so a `WORKER_LOST`
    window carries the last beats then silence - which is what the detector's
    age rule reads.
    """
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
    """Heartbeats stop for real while the queue holds work (plan §6.2).

    The last beat sits far enough below the staleness threshold that at
    ``detectable_at`` the derived age clears the detector's *strict* inequality
    with margin: at onset the age reads exactly one threshold, so a tick too
    early is correctly silent rather than borderline.
    """
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
    """A burst larger than the single worker can drain (plan §6.2).

    No dedicated rule owns a backlog, so the window's label is empty: its
    designed detection path is the residual `METRIC_SHIFT` once the queue
    baselines are warm, and its honest cold-start outcome is silence. The
    worker is alive - it carries fresh heartbeats, so the window must not be
    scored as a lost worker or reported blind.
    """
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
    """A null window: healthy heartbeats, a moving queue, nothing wrong.

    Like every builder, `detectable_at` is the window close; scoring a null
    window there is the honest "the tick that would have seen this window said
    nothing".
    """
    records = heartbeat_records(count=3, end_at=onset)
    if warm:
        # Benign progress so the window does not read as "nothing is running".
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
    """The production monitor with two replay seams and no other overrides.

    `_records` is overridden to return the injected telemetry (the real service
    reads a JSONL sink; replay hands the records over directly - the sink path
    itself is exercised by its own tests). `_probe` is overridden only when a
    window injects a snapshot; otherwise the real probe runs against whatever
    catalog the service was built with.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._chaos_records: list[dict[str, Any]] = []
        self._chaos_snapshot: dict[str, Any] | None = None

    def inject(self, window: FaultWindow) -> None:
        """Arm the seams for the next tick; call once per window."""
        self._chaos_records = list(window.records)
        self._chaos_snapshot = window.snapshot

    def _records(self, reference: datetime) -> list[dict[str, Any]]:  # noqa: ARG002
        # `reference` is the override's contract (the service calls it with the
        # tick time); replay ignores it, so the injected window alone decides.
        return list(self._chaos_records)

    def _probe(self, reference: datetime, catalog: Any) -> tuple[dict[str, Any], bool]:
        if self._chaos_snapshot is not None:
            return dict(self._chaos_snapshot), True
        return super()._probe(reference, catalog)


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
    """Run one tick against an armed window and score it.

    The tick time defaults to the window's `detectable_at`: the earliest moment
    the aggregation window containing the fault has closed. Scoring a fault at
    its onset would credit detection the pipeline cannot have. The service's
    clock is pinned to that instant for the tick, so features, sink lag, and
    latency are all computed against the same time the outcome is scored at.
    """
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
    """Run windows back to back on one monitor, in replay order.

    Baselines keep updating through the real `_update_baselines` between
    windows, so cold-start abstention behaves exactly as in production:
    statistical rules stay silent until scopes are warm. Windows must be given
    in increasing `detectable_at` order - a replay that runs backwards would
    score detection latency against a clock that never existed.
    """
    outcomes: list[WindowOutcome] = []
    previous: datetime | None = None
    for window in windows:
        if previous is not None and window.detectable_at < previous:
            raise ValueError("windows must be ordered by detectable_at")
        previous = window.detectable_at
        outcomes.append(evaluate_window(monitor, window))
    return outcomes


__all__ = [
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
