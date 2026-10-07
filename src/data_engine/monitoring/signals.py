"""Severity, notification class, and the signal record every detector emits."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from data_engine.monitoring.features import FEATURE_SCHEMA_VERSION


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"


class NotifyClass(StrEnum):
    NOTIFY = "notify"
    QUEUE = "queue"


SEVERITY_ORDER: dict[str, int] = {s.value: i for i, s in enumerate(Severity)}


class Label(StrEnum):
    """The taxonomy, re-derived for unattended local-first curation."""

    CONTRACT_BREACH = "CONTRACT_BREACH"
    RUN_STALLED = "RUN_STALLED"
    WORKER_LOST = "WORKER_LOST"
    DISK_PRESSURE = "DISK_PRESSURE"
    DATABASE_UNREACHABLE = "DATABASE_UNREACHABLE"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    QUARANTINE_RATE_HIGH = "QUARANTINE_RATE_HIGH"
    QUALITY_SHIFT = "QUALITY_SHIFT"
    FRAME_COUNT_COLLAPSE = "FRAME_COUNT_COLLAPSE"
    CLOCK_DRIFT = "CLOCK_DRIFT"
    REPEATED_READ_FAILURE = "REPEATED_READ_FAILURE"
    TIME_MISSED = "TIME_MISSED"
    METRIC_SHIFT = "METRIC_SHIFT"
    RESOURCE_DEGRADED = "RESOURCE_DEGRADED"


NOTIFY_LABELS: frozenset[str] = frozenset(
    {
        Label.CONTRACT_BREACH.value,
        Label.RUN_STALLED.value,
        Label.WORKER_LOST.value,
        Label.DISK_PRESSURE.value,
        Label.DATABASE_UNREACHABLE.value,
        Label.CLOCK_DRIFT.value,
    }
)

NOTIFY_SEVERITY_FLOOR = Severity.CRITICAL


@dataclass(frozen=True, slots=True)
class Signal:
    """One detector's finding, carrying the evidence that justifies it."""

    label: str
    severity: Severity
    scope: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    notify: NotifyClass | None = None
    feature_schema_version: int = FEATURE_SCHEMA_VERSION

    @property
    def notify_class(self) -> NotifyClass:
        if self.notify is not None:
            return self.notify
        return NotifyClass.NOTIFY if self.label in NOTIFY_LABELS else NotifyClass.QUEUE

    def to_evidence(self) -> dict[str, Any]:
        """The persisted evidence bundle, shared by incidents and contracts."""
        return {
            "label": self.label,
            "severity": self.severity.value,
            "scope": self.scope,
            "detail": self.detail,
            "evidence": dict(self.evidence),
            "notify_class": self.notify_class.value,
            "feature_schema_version": self.feature_schema_version,
        }


def validate_label(value: str) -> str:
    """Reject an unknown label at the API boundary (422), not deep in triage."""
    try:
        return Label(value).value
    except ValueError as exc:
        known = ", ".join(sorted(item.value for item in Label))
        raise ValueError(f"unknown incident label: {value} (known: {known})") from exc
