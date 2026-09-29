"""Severity, notification class, and the signal record every detector emits.

Two vocabularies live here, and keeping them apart is the point.

**Severity is the cost of the night, not the size of the deviation** (ADR 0020).
On a fleet, severity is a property of the metric's blast radius. On one laptop
that the owner has walked away from, it is a property of what happens if nobody
acts: is the compute being wasted, is data being lost, or is a stated commitment
being missed? A 3-sigma wobble in a metric nobody is waiting on is *less* urgent
than a run that will never finish, and a severity model that says otherwise would
wake someone for the wrong thing.

**Notify class is the channel decision**, and it is far stricter than severity.
Most incidents are ones the owner would find tomorrow by looking; those queue.
Only three situations justify interrupting an absent person, and they are
enumerated in :data:`NOTIFY_LABELS` so the policy is auditable in one place.
"""

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


#: Labels that justify interrupting an absent operator. The three situations are
#: "the night is being wasted", "data is being lost as we speak", and "a
#: commitment was missed". Everything else batches (automation plan §10.1).
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

#: Defaults used when a detector does not state a channel decision explicitly.
NOTIFY_SEVERITY_FLOOR = Severity.CRITICAL


@dataclass(frozen=True, slots=True)
class Signal:
    """One detector's finding, carrying the evidence that justifies it.

    ``evidence`` must be enough for a human to check the claim by hand. A signal
    that cannot cite its numbers does not get to open an incident — that rule is
    enforced at the triage boundary, not trusted to each detector.
    """

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
