"""Triage: fingerprinting, dedup, cooldown, and the hard alert budget.

This is where most of the system's precision comes from. Precision is a property
of the whole pipeline rather than of any single rule, and a detector that fires
correctly fifty times a minute still destroys the channel. So every signal passes
through here, and a signal that cannot earn its place is dropped with a recorded
reason — never silently, because a suppressed signal nobody can audit is
indistinguishable from a bug.

The gates, in order:

1. **Evidence gate.** A signal with an empty evidence bundle is rejected. The
   promise is that no incident exists which cannot cite the numbers behind it, and
   this is where that promise is enforced rather than trusted to each rule.
2. **Severity gate.** Below ``min_severity`` is not an incident.
3. **Dedup.** One fingerprint means one open incident, no matter how many rules or
   ticks noticed the same fault. A repeat is a *bump*: the counter and the
   timestamp move, a new row does not appear.
4. **Cooldown.** A resolved incident that re-fires inside the cooldown is
   suppressed, so a flapping fault cannot manufacture a stream of rows. This is
   also the measurement behind the time-to-acknowledge proxy: the gap before a
   recurrence opens a genuinely new incident.
5. **Budget.** Past ``budget_per_window`` new incidents, the window degrades to
   log-only and *says so*. A monitor that quietly floods is indistinguishable from
   one that has stopped working.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from data_engine.monitoring.signals import Label, Severity, Signal


class TriageAction(StrEnum):
    OPEN = "open"
    BUMP = "bump"
    SUPPRESS_NO_EVIDENCE = "suppress_no_evidence"
    SUPPRESS_SEVERITY = "suppress_severity"
    SUPPRESS_COOLDOWN = "suppress_cooldown"
    SUPPRESS_BUDGET = "suppress_budget"


@dataclass(frozen=True, slots=True)
class TriagePolicy:
    """The alert budget, stated in one auditable place."""

    cooldown_seconds: float = 1800.0
    min_severity: Severity = Severity.MEDIUM
    budget_per_window: int = 10

    def severity_ok(self, severity: Severity) -> bool:
        order = [Severity.INFO, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        return order.index(severity) >= order.index(self.min_severity)


@dataclass(frozen=True, slots=True)
class ExistingIncident:
    """The minimum an incident needs for triage to reason about recurrence."""

    id: str
    fingerprint: str
    status: str
    last_seen: datetime
    occurrence_count: int = 1

    @property
    def unresolved(self) -> bool:
        return self.status != "resolved"


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    action: TriageAction
    signal: Signal
    fingerprint: str
    incident: ExistingIncident | None = None
    reason: str = ""


#: Evidence keys that identify *which* fault fired and are therefore part of the
#: fingerprint. Numbers are excluded on purpose: they change every tick, and
#: including them would make dedup impossible.
_STABLE_EVIDENCE_KEYS: frozenset[str] = frozenset(
    {"source", "reason_code", "job_type", "profile_name"}
)


def signature(signal: Signal) -> str:
    """A coarse, stable descriptor of the fault — never the numbers in it."""
    if signal.label == Label.METRIC_SHIFT.value:
        shifted = signal.evidence.get("shifted") or []
        return ",".join(sorted(str(item.get("feature")) for item in shifted))
    if signal.label == Label.QUALITY_SHIFT.value:
        return str(signal.evidence.get("feature", ""))
    return ",".join(
        f"{key}={signal.evidence[key]}"
        for key in sorted(_STABLE_EVIDENCE_KEYS)
        if isinstance(signal.evidence.get(key), str)
    )


def fingerprint(signal: Signal) -> str:
    """Stable identity for one fault: label + scope + coarse signature."""
    material = f"{signal.label}|{signal.scope}|{signature(signal)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def decide(
    signals: Iterable[Signal],
    *,
    now: datetime,
    policy: TriagePolicy,
    open_incidents: Mapping[str, ExistingIncident],
    budget_used: int = 0,
) -> list[TriageOutcome]:
    """Route signals to open/bump/suppress. Order is preserved for stable reports."""
    outcomes: list[TriageOutcome] = []
    budget = budget_used
    for signal in signals:
        identity = fingerprint(signal)

        if not signal.evidence:
            outcomes.append(
                TriageOutcome(
                    action=TriageAction.SUPPRESS_NO_EVIDENCE,
                    signal=signal,
                    fingerprint=identity,
                    reason="signal carries no evidence",
                )
            )
            continue

        if not policy.severity_ok(signal.severity):
            outcomes.append(
                TriageOutcome(
                    action=TriageAction.SUPPRESS_SEVERITY,
                    signal=signal,
                    fingerprint=identity,
                    reason=(
                        f"{signal.severity.value} is below the {policy.min_severity.value} floor"
                    ),
                )
            )
            continue

        existing = open_incidents.get(identity)
        if existing is not None and existing.unresolved:
            outcomes.append(
                TriageOutcome(
                    action=TriageAction.BUMP,
                    signal=signal,
                    fingerprint=identity,
                    incident=existing,
                    reason=(
                        f"open incident {existing.id}, occurrence {existing.occurrence_count + 1}"
                    ),
                )
            )
            continue

        if existing is not None and now - existing.last_seen < timedelta(
            seconds=policy.cooldown_seconds
        ):
            outcomes.append(
                TriageOutcome(
                    action=TriageAction.SUPPRESS_COOLDOWN,
                    signal=signal,
                    fingerprint=identity,
                    incident=existing,
                    reason="same fault recurred inside the cooldown window",
                )
            )
            continue

        if budget >= policy.budget_per_window:
            outcomes.append(
                TriageOutcome(
                    action=TriageAction.SUPPRESS_BUDGET,
                    signal=signal,
                    fingerprint=identity,
                    reason=(
                        f"alert budget of {policy.budget_per_window} reached for this window; "
                        "triage is log-only"
                    ),
                )
            )
            continue

        budget += 1
        outcomes.append(
            TriageOutcome(
                action=TriageAction.OPEN,
                signal=signal,
                fingerprint=identity,
                reason="new fault",
            )
        )
    return outcomes


def opened(outcomes: Sequence[TriageOutcome]) -> list[TriageOutcome]:
    return [outcome for outcome in outcomes if outcome.action == TriageAction.OPEN]


def suppressed(outcomes: Sequence[TriageOutcome]) -> list[TriageOutcome]:
    return [outcome for outcome in outcomes if outcome.action.value.startswith("suppress")]


def budget_exhausted(outcomes: Sequence[TriageOutcome]) -> bool:
    return any(outcome.action == TriageAction.SUPPRESS_BUDGET for outcome in outcomes)


def reasons(outcomes: Iterable[TriageOutcome]) -> dict[str, int]:
    """Suppression counts by action, for the monitor's own health readout."""
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.action.value] = counts.get(outcome.action.value, 0) + 1
    return counts


def as_mapping(rows: Sequence[Mapping[str, Any]]) -> dict[str, ExistingIncident]:
    """Index catalog rows by fingerprint for :func:`decide`."""
    return {
        str(row["fingerprint"]): ExistingIncident(
            id=str(row["id"]),
            fingerprint=str(row["fingerprint"]),
            status=str(row.get("status") or "open"),
            last_seen=row["last_seen"],
            occurrence_count=int(row.get("occurrence_count") or 1),
        )
        for row in rows
    }
