"""Unit tests for triage: fingerprinting, the gates, dedup, cooldown, budget.

Most of the system's precision is decided here rather than in any detector, so
these tests are about *what gets suppressed* as much as about what opens.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from data_engine.monitoring.queue import (
    ExistingIncident,
    TriageAction,
    TriagePolicy,
    as_mapping,
    budget_exhausted,
    decide,
    fingerprint,
    reasons,
    signature,
)
from data_engine.monitoring.signals import Label, Severity, Signal

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
POLICY = TriagePolicy()


def _signal(
    label: str = Label.WORKER_LOST.value,
    *,
    severity: Severity = Severity.CRITICAL,
    scope: str = "worker",
    evidence: dict[str, Any] | None = None,
) -> Signal:
    return Signal(
        label=label,
        severity=severity,
        scope=scope,
        detail="test",
        evidence=evidence if evidence is not None else {"heartbeat_age_seconds": 900.0},
    )


def _decide(
    signals: list[Signal],
    *,
    existing: dict[str, ExistingIncident] | None = None,
    policy: TriagePolicy = POLICY,
    budget_used: int = 0,
    now: datetime = NOW,
) -> list[Any]:
    return decide(
        signals, now=now, policy=policy, open_incidents=existing or {}, budget_used=budget_used
    )


class TestFingerprint:
    def test_is_stable_across_calls(self) -> None:
        assert fingerprint(_signal()) == fingerprint(_signal())

    def test_ignores_the_numbers_that_change_every_tick(self) -> None:
        # Including the evidence values would make dedup impossible: the whole
        # point is that a still-open fault is recognised despite its numbers moving.
        first = _signal(evidence={"heartbeat_age_seconds": 900.0})
        second = _signal(evidence={"heartbeat_age_seconds": 1200.0})
        assert fingerprint(first) == fingerprint(second)

    def test_separates_different_labels(self) -> None:
        assert fingerprint(_signal(Label.WORKER_LOST.value)) != fingerprint(
            _signal(Label.DATABASE_UNREACHABLE.value)
        )

    def test_separates_different_scopes(self) -> None:
        # Two stalled jobs are two incidents, not one.
        assert fingerprint(_signal(scope="job-1")) != fingerprint(_signal(scope="job-2"))

    def test_includes_stable_identifying_evidence(self) -> None:
        one = _signal(Label.REPEATED_READ_FAILURE.value, scope="x", evidence={"reason_code": "A"})
        two = _signal(Label.REPEATED_READ_FAILURE.value, scope="x", evidence={"reason_code": "B"})
        assert fingerprint(one) != fingerprint(two)

    def test_metric_shift_is_keyed_on_which_features_moved(self) -> None:
        def shifted(*names: str) -> Signal:
            return _signal(
                Label.METRIC_SHIFT.value,
                scope="platform",
                evidence={"shifted": [{"feature": name} for name in names]},
            )

        assert fingerprint(shifted("a", "b")) == fingerprint(shifted("b", "a"))
        assert fingerprint(shifted("a", "b")) != fingerprint(shifted("a", "c"))

    def test_quality_shift_is_keyed_on_the_feature(self) -> None:
        one = _signal(
            Label.QUALITY_SHIFT.value, scope="jerk_mean", evidence={"feature": "jerk_mean"}
        )
        two = _signal(
            Label.QUALITY_SHIFT.value, scope="stall_mean", evidence={"feature": "stall_mean"}
        )
        assert fingerprint(one) != fingerprint(two)

    def test_signature_is_empty_without_identifying_evidence(self) -> None:
        assert signature(_signal(evidence={"heartbeat_age_seconds": 1.0})) == ""


class TestEvidenceGate:
    def test_a_signal_without_evidence_is_rejected(self) -> None:
        # The promise is that no incident exists which cannot cite its numbers.
        outcome = _decide([_signal(evidence={})])[0]
        assert outcome.action == TriageAction.SUPPRESS_NO_EVIDENCE
        assert "evidence" in outcome.reason


class TestSeverityGate:
    def test_below_the_floor_is_suppressed(self) -> None:
        outcome = _decide([_signal(severity=Severity.INFO)])[0]
        assert outcome.action == TriageAction.SUPPRESS_SEVERITY

    def test_at_the_floor_is_kept(self) -> None:
        outcome = _decide([_signal(severity=Severity.MEDIUM)])[0]
        assert outcome.action == TriageAction.OPEN

    def test_the_floor_is_configurable(self) -> None:
        outcome = _decide(
            [_signal(severity=Severity.HIGH)], policy=TriagePolicy(min_severity=Severity.CRITICAL)
        )[0]
        assert outcome.action == TriageAction.SUPPRESS_SEVERITY


class TestDedup:
    def test_a_new_fault_opens(self) -> None:
        assert _decide([_signal()])[0].action == TriageAction.OPEN

    def test_a_repeat_bumps_instead_of_opening(self) -> None:
        identity = fingerprint(_signal())
        existing = {
            identity: ExistingIncident(
                id="inc-1", fingerprint=identity, status="open", last_seen=NOW, occurrence_count=3
            )
        }
        outcome = _decide([_signal()], existing=existing)[0]
        assert outcome.action == TriageAction.BUMP
        assert outcome.incident is not None
        assert outcome.incident.id == "inc-1"
        assert "occurrence 4" in outcome.reason

    def test_an_acknowledged_incident_still_dedups(self) -> None:
        # Acknowledging is not resolving: the fault has not stopped.
        identity = fingerprint(_signal())
        existing = {
            identity: ExistingIncident(
                id="inc-1", fingerprint=identity, status="acknowledged", last_seen=NOW
            )
        }
        assert _decide([_signal()], existing=existing)[0].action == TriageAction.BUMP

    def test_a_different_fault_is_a_different_incident(self) -> None:
        identity = fingerprint(_signal())
        existing = {
            identity: ExistingIncident(
                id="inc-1", fingerprint=identity, status="open", last_seen=NOW
            )
        }
        outcome = _decide([_signal(scope="other")], existing=existing)[0]
        assert outcome.action == TriageAction.OPEN


class TestCooldown:
    def test_a_resolved_fault_recurs_inside_the_cooldown_is_suppressed(self) -> None:
        identity = fingerprint(_signal())
        existing = {
            identity: ExistingIncident(
                id="inc-1",
                fingerprint=identity,
                status="resolved",
                last_seen=NOW - timedelta(minutes=5),
            )
        }
        outcome = _decide([_signal()], existing=existing)[0]
        assert outcome.action == TriageAction.SUPPRESS_COOLDOWN

    def test_a_resolved_fault_recurs_after_the_cooldown_opens_a_new_incident(self) -> None:
        # This gap is the time-to-acknowledge proxy the plan reports on.
        identity = fingerprint(_signal())
        existing = {
            identity: ExistingIncident(
                id="inc-1",
                fingerprint=identity,
                status="resolved",
                last_seen=NOW - timedelta(hours=2),
            )
        }
        assert _decide([_signal()], existing=existing)[0].action == TriageAction.OPEN

    def test_the_cooldown_is_configurable(self) -> None:
        identity = fingerprint(_signal())
        existing = {
            identity: ExistingIncident(
                id="inc-1", fingerprint=identity, status="resolved", last_seen=NOW
            )
        }
        outcome = _decide(
            [_signal()], existing=existing, policy=TriagePolicy(cooldown_seconds=0.0)
        )[0]
        assert outcome.action == TriageAction.OPEN


class TestBudget:
    def test_the_budget_stops_new_incidents(self) -> None:
        outcomes = _decide(
            [_signal(scope="a"), _signal(scope="b")],
            policy=TriagePolicy(budget_per_window=1),
        )
        assert [o.action for o in outcomes] == [TriageAction.OPEN, TriageAction.SUPPRESS_BUDGET]

    def test_exhaustion_is_reported_rather_than_hidden(self) -> None:
        outcomes = _decide([_signal()], policy=TriagePolicy(budget_per_window=0))
        assert budget_exhausted(outcomes)
        assert "log-only" in outcomes[0].reason

    def test_a_bump_does_not_consume_budget(self) -> None:
        # Re-firing an open incident is not news; only new rows are.
        identity = fingerprint(_signal())
        existing = {
            identity: ExistingIncident(
                id="inc-1", fingerprint=identity, status="open", last_seen=NOW
            )
        }
        outcomes = _decide(
            [_signal(), _signal(scope="other")],
            existing=existing,
            policy=TriagePolicy(budget_per_window=1),
        )
        assert [o.action for o in outcomes] == [TriageAction.BUMP, TriageAction.OPEN]

    def test_budget_used_from_the_window_is_honoured(self) -> None:
        outcome = _decide([_signal()], budget_used=10, policy=TriagePolicy(budget_per_window=10))[0]
        assert outcome.action == TriageAction.SUPPRESS_BUDGET


class TestReporting:
    def test_reasons_counts_every_action(self) -> None:
        outcomes = _decide(
            [
                _signal(scope="a"),
                _signal(scope="b"),
                _signal(scope="a", evidence={}),
                _signal(scope="c", severity=Severity.INFO),
            ]
        )
        counts = reasons(outcomes)
        assert counts["open"] == 2
        assert counts["suppress_no_evidence"] == 1
        assert counts["suppress_severity"] == 1

    def test_as_mapping_indexes_rows_by_fingerprint(self) -> None:
        rows = [
            {
                "id": "inc-1",
                "fingerprint": "abc",
                "status": "open",
                "last_seen": NOW,
                "occurrence_count": 2,
            }
        ]
        mapping = as_mapping(rows)
        assert mapping["abc"].occurrence_count == 2
        assert mapping["abc"].unresolved

    def test_a_resolved_row_is_marked_resolved(self) -> None:
        rows = [{"id": "i", "fingerprint": "abc", "status": "resolved", "last_seen": NOW}]
        assert not as_mapping(rows)["abc"].unresolved

    def test_empty_input_is_empty_output(self) -> None:
        assert _decide([]) == []
        assert reasons([]) == {}
