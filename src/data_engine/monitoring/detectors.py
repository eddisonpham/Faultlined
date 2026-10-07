"""Deterministic detectors: explicit rules over the feature vector (ADR 0020)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from data_engine.monitoring.baselines import BaselineBook
from data_engine.monitoring.features import FEATURE_NAMES, FeatureVector
from data_engine.monitoring.signals import Label, NotifyClass, Severity, Signal

GIB = 1024**3


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Every threshold in the notifier, in one auditable place."""

    heartbeat_stale_seconds: float = 180.0
    stall_seconds: float = 900.0
    disk_free_floor_bytes: float = 2 * GIB
    clock_gap_seconds: float = 5.0
    quarantine_absolute_max: float = 0.35
    quarantine_z: float = 4.0
    quality_z: float = 4.0
    frame_collapse_ratio: float = 0.5
    repeated_failure_count: float = 3.0
    cpu_high_percent: float = 90.0
    memory_high_ratio: float = 0.92
    metric_shift_z: float = 4.0
    metric_shift_min_features: int = 2
    watched: tuple[tuple[str, str], ...] = (
        ("queue_oldest_age_seconds", "high"),
        ("queue_depth_queued", "high"),
        ("episodes_per_second", "low"),
        ("catalog_query_p95_seconds", "high"),
        ("api_request_p95_seconds", "high"),
        ("api_error_rate", "high"),
        ("job_retry_count", "high"),
        ("job_timeout_count", "high"),
    )


DEFAULT_CONFIG = DetectorConfig()

SPECIALISED: frozenset[str] = frozenset(
    {
        "quarantine_rate",
        "verdict_jerky_rate",
        "jerk_mean",
        "stall_mean",
        "frame_count_median",
        "cpu_percent",
        "memory_used_ratio",
        "disk_free_bytes",
        "heartbeat_age_seconds",
    }
)


@dataclass(frozen=True, slots=True)
class DetectorContext:
    """Everything a detector may look at."""

    now: datetime
    features: FeatureVector
    baselines: BaselineBook
    running: Sequence[Mapping[str, Any]] = ()
    config: DetectorConfig = DEFAULT_CONFIG

    @property
    def work_outstanding(self) -> bool:
        """True when something is queued or running, or the last window produced work."""
        features = self.features
        return bool(
            features.value("queue_depth_queued")
            or features.value("queue_depth_running")
            or features.value("episodes_ingested")
            or self.running
        )


Detector = Callable[[DetectorContext], list[Signal]]


def detect(context: DetectorContext) -> list[Signal]:
    """Run every rule, in registry order."""
    signals: list[Signal] = []
    for _name, rule in DETECTORS:
        signals.extend(rule(context))
    return signals


def database_unreachable(context: DetectorContext) -> list[Signal]:
    if context.features.catalog_reachable:
        return []
    return [
        Signal(
            label=Label.DATABASE_UNREACHABLE.value,
            severity=Severity.CRITICAL,
            scope="catalog",
            detail="the catalog probe failed; monitoring is running blind",
            evidence={"probe": "catalog_snapshot"},
            notify=NotifyClass.NOTIFY,
        )
    ]


def worker_lost(context: DetectorContext) -> list[Signal]:
    """No heartbeat while work is outstanding."""
    if not context.work_outstanding or not context.features.catalog_reachable:
        return []
    features = context.features
    if features.has("heartbeat_age_seconds"):
        age = features.value("heartbeat_age_seconds")
        if age <= context.config.heartbeat_stale_seconds:
            return []
        evidence: dict[str, Any] = {
            "heartbeat_age_seconds": age,
            "threshold_seconds": context.config.heartbeat_stale_seconds,
            "queue_depth_queued": features.value("queue_depth_queued"),
            "queue_depth_running": features.value("queue_depth_running"),
        }
        detail = (
            f"no worker heartbeat for {age:.0f}s while work is outstanding "
            f"(laptop suspend, dead worker, or a closed terminal)"
        )
    else:
        evidence = {
            "heartbeat_age_seconds": None,
            "threshold_seconds": context.config.heartbeat_stale_seconds,
            "note": "no heartbeat record has been observed in the window",
        }
        detail = "no worker heartbeat observed while work is outstanding"
    return [
        Signal(
            label=Label.WORKER_LOST.value,
            severity=Severity.CRITICAL,
            scope="worker",
            detail=detail,
            evidence=evidence,
            notify=NotifyClass.NOTIFY,
        )
    ]


def run_stalled(context: DetectorContext) -> list[Signal]:
    """A run that is still going, far past any plausible duration, with no progress."""
    config = context.config
    progress = context.features.value("episodes_ingested")
    signals: list[Signal] = []
    for job in context.running:
        age = float(job.get("age_seconds") or 0.0)
        if age < config.stall_seconds:
            continue
        deadline_passed = bool(job.get("deadline_passed"))
        if not deadline_passed and progress > 0:
            continue
        signals.append(
            Signal(
                label=Label.RUN_STALLED.value,
                severity=Severity.CRITICAL,
                scope=str(job.get("id") or "unknown"),
                detail=(
                    f"run has been going {age:.0f}s"
                    + (" past its deadline" if deadline_passed else " with no episodes produced")
                ),
                evidence={
                    "job_id": job.get("id"),
                    "job_type": job.get("job_type"),
                    "age_seconds": age,
                    "stall_threshold_seconds": config.stall_seconds,
                    "deadline_passed": deadline_passed,
                    "episodes_in_window": progress,
                },
                notify=NotifyClass.NOTIFY,
            )
        )
    return signals


def disk_pressure(context: DetectorContext) -> list[Signal]:
    """Free space below the floor."""
    features = context.features
    if not features.has("disk_free_bytes"):
        return []
    free = features.value("disk_free_bytes")
    if free >= context.config.disk_free_floor_bytes:
        return []
    return [
        Signal(
            label=Label.DISK_PRESSURE.value,
            severity=Severity.CRITICAL,
            scope="host",
            detail=(
                f"{free / GIB:.1f} GiB free, below the "
                f"{context.config.disk_free_floor_bytes / GIB:.0f} GiB floor; "
                "artifact publishing will fail"
            ),
            evidence={
                "disk_free_bytes": free,
                "threshold_bytes": context.config.disk_free_floor_bytes,
            },
            notify=NotifyClass.NOTIFY,
        )
    ]


def clock_drift(context: DetectorContext) -> list[Signal]:
    """A gap inside one episode's timestamps."""
    gap = context.features.value("max_episode_timestamp_gap_seconds", -1.0)
    if gap < context.config.clock_gap_seconds:
        return []
    return [
        Signal(
            label=Label.CLOCK_DRIFT.value,
            severity=Severity.CRITICAL,
            scope=context.features.ref("timestamp_gap_scope", "unknown"),
            detail=(
                f"largest gap inside an episode is {gap:.1f}s; camera and joint streams are "
                "not aligned"
            ),
            evidence={
                "max_gap_seconds": gap,
                "threshold_seconds": context.config.clock_gap_seconds,
                "source": context.features.ref("timestamp_gap_source"),
            },
            notify=NotifyClass.NOTIFY,
        )
    ]


def quarantine_rate_high(context: DetectorContext) -> list[Signal]:
    """More episodes quarantined than this source normally loses."""
    features, config = context.features, context.config
    if not features.has("quarantine_rate"):
        return []
    rate = features.value("quarantine_rate")
    baseline = context.baselines.get("quarantine_rate")
    evidence: dict[str, Any] = {
        "quarantine_rate": rate,
        "absolute_max": config.quarantine_absolute_max,
        "warm": bool(baseline and baseline.warm()),
    }
    if rate > config.quarantine_absolute_max:
        basis = "absolute"
    elif baseline is not None and baseline.warm() and baseline.robust_z(rate) > config.quarantine_z:
        basis = "baseline"
    else:
        return []
    evidence["basis"] = basis
    if basis == "baseline" and baseline is not None:
        evidence["baseline_median"] = baseline.median
        evidence["robust_z"] = baseline.robust_z(rate)
    else:
        evidence.setdefault("baseline_median", None)
    return [
        Signal(
            label=Label.QUARANTINE_RATE_HIGH.value,
            severity=Severity.HIGH,
            scope="curation",
            detail=f"{rate:.0%} of evaluated episodes are quarantined ({basis} bound)",
            evidence=evidence,
        )
    ]


def quality_shift(context: DetectorContext) -> list[Signal]:
    """The motion-quality distribution moved relative to its own history."""
    config = context.config
    for feature, human in (
        ("verdict_jerky_rate", "jerky episodes"),
        ("jerk_mean", "mean jerk"),
        ("stall_mean", "mean stall ratio"),
    ):
        baseline = context.baselines.get(feature)
        if not context.features.has(feature) or baseline is None or not baseline.warm():
            continue
        value = context.features.value(feature)
        z = baseline.robust_z(value)
        if z <= config.quality_z:
            continue
        return [
            Signal(
                label=Label.QUALITY_SHIFT.value,
                severity=Severity.HIGH,
                scope=feature,
                detail=(
                    f"{human} at {value:.3f} is {z:.1f} robust sigmas above this platform's "
                    f"usual {baseline.median:.3f}"
                ),
                evidence={
                    "feature": feature,
                    "value": value,
                    "baseline_median": baseline.median,
                    "baseline_sigma": baseline.sigma,
                    "robust_z": z,
                    "threshold_sigma": config.quality_z,
                    "observations": baseline.observations,
                },
            )
        ]
    return []


def frame_count_collapse(context: DetectorContext) -> list[Signal]:
    """Episodes arriving far shorter than this source normally produces."""
    config = context.config
    baseline = context.baselines.get("frame_count_median")
    if not context.features.has("frame_count_median") or baseline is None or not baseline.warm():
        return []
    value = context.features.value("frame_count_median")
    ratio = baseline.ratio_to_center(value)
    if ratio >= config.frame_collapse_ratio:
        return []
    return [
        Signal(
            label=Label.FRAME_COUNT_COLLAPSE.value,
            severity=Severity.HIGH,
            scope="frame_count_median",
            detail=(
                f"median episode length is {value:.0f} frames, {ratio:.0%} of the usual "
                f"{baseline.center:.0f}"
            ),
            evidence={
                "value": value,
                "baseline_center": baseline.center,
                "ratio_to_center": ratio,
                "threshold_ratio": config.frame_collapse_ratio,
                "observations": baseline.observations,
            },
        )
    ]


def repeated_read_failure(context: DetectorContext) -> list[Signal]:
    """The same reason code failing over and over in one window."""
    threshold = context.config.repeated_failure_count
    signals: list[Signal] = []
    for reason, count in context.features.scoped_pairs("failure_count"):
        if count < threshold:
            continue
        signals.append(
            Signal(
                label=Label.REPEATED_READ_FAILURE.value,
                severity=Severity.HIGH,
                scope=reason,
                detail=f"{int(count)} failures with reason {reason} in one window",
                evidence={"reason_code": reason, "failure_count": count, "threshold": threshold},
            )
        )
    return signals


def resource_degraded(context: DetectorContext) -> list[Signal]:
    """Sustained host pressure."""
    config = context.config
    features = context.features
    pressure: list[str] = []
    if features.has("cpu_percent") and features.value("cpu_percent") > config.cpu_high_percent:
        pressure.append(f"cpu {features.value('cpu_percent'):.0f}%")
    if (
        features.has("memory_used_ratio")
        and features.value("memory_used_ratio") > config.memory_high_ratio
    ):
        pressure.append(f"memory {features.value('memory_used_ratio'):.0%}")
    if not pressure:
        return []
    return [
        Signal(
            label=Label.RESOURCE_DEGRADED.value,
            severity=Severity.MEDIUM,
            scope="host",
            detail=f"host under pressure: {', '.join(pressure)}",
            evidence={
                "cpu_percent": features.value("cpu_percent"),
                "memory_used_ratio": features.value("memory_used_ratio"),
                "cpu_threshold": config.cpu_high_percent,
                "memory_threshold": config.memory_high_ratio,
            },
        )
    ]


def metric_shift(context: DetectorContext) -> list[Signal]:
    """Several residual features beyond their own limits in the same window."""
    config = context.config
    shifted: list[dict[str, Any]] = []
    for feature, direction in config.watched:
        if feature in SPECIALISED:
            continue
        baseline = context.baselines.get(feature)
        if not context.features.has(feature) or baseline is None or not baseline.warm():
            continue
        value = context.features.value(feature)
        z = baseline.robust_z(value)
        if direction == "high" and z <= config.metric_shift_z:
            continue
        if direction == "low" and z >= -config.metric_shift_z:
            continue
        shifted.append(
            {
                "feature": feature,
                "direction": direction,
                "value": value,
                "baseline_median": baseline.median,
                "robust_z": z,
            }
        )
    if len(shifted) < config.metric_shift_min_features:
        return []
    shifted.sort(key=lambda item: (-abs(float(item["robust_z"])), str(item["feature"])))
    names = ", ".join(
        f"{item['feature']}={float(item['value']):.4g} ({float(item['robust_z']):+.1f} sigmas)"
        for item in shifted
    )
    return [
        Signal(
            label=Label.METRIC_SHIFT.value,
            severity=Severity.MEDIUM,
            scope="platform",
            detail=f"{len(shifted)} signals moved together: {names}",
            evidence={
                "shifted": shifted,
                "min_features": config.metric_shift_min_features,
                "threshold_sigma": config.metric_shift_z,
            },
        )
    ]


DETECTORS: tuple[tuple[str, Detector], ...] = (
    ("database_unreachable", database_unreachable),
    ("worker_lost", worker_lost),
    ("run_stalled", run_stalled),
    ("disk_pressure", disk_pressure),
    ("clock_drift", clock_drift),
    ("quarantine_rate_high", quarantine_rate_high),
    ("quality_shift", quality_shift),
    ("frame_count_collapse", frame_count_collapse),
    ("repeated_read_failure", repeated_read_failure),
    ("resource_degraded", resource_degraded),
    ("metric_shift", metric_shift),
)


def observations_for(
    features: FeatureVector, *, exclude: frozenset[str] = frozenset()
) -> list[tuple[str, float, str]]:
    """``(feature, value, scope)`` triples to feed the baselines after a tick."""
    triples: list[tuple[str, float, str]] = []
    for name in FEATURE_NAMES:
        if name in exclude or not features.has(name):
            continue
        triples.append((name, features.value(name), ""))
    for scope, value in features.scoped_pairs("run_time_p95_seconds"):
        triples.append(("run_time_p95_seconds", value, scope))
    return triples


__all__ = [
    "DEFAULT_CONFIG",
    "DETECTORS",
    "GIB",
    "DetectorConfig",
    "DetectorContext",
    "detect",
    "observations_for",
]
