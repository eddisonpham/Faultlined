"""Unit tests for completion contracts (ADR 0020 §8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from data_engine.monitoring.contracts import (
    OUTCOME_DEGRADED,
    OUTCOME_MET,
    OUTCOME_MISSED,
    OUTCOME_PENDING,
    OUTCOME_SHORT,
    Expectation,
    InvalidExpectation,
    RunOutcome,
    evaluate,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _settled(produced: int, valid: int, **kwargs: Any) -> RunOutcome:
    fields: dict[str, Any] = {
        "job_id": "job-1",
        "state": "succeeded",
        "settled": True,
        "episodes_produced": produced,
        "episodes_valid": valid,
        "duration_seconds": 1.0,
        "now": NOW,
    }
    fields.update(kwargs)
    return RunOutcome(**fields)


def _unsettled(state: str = "queued", **kwargs: Any) -> RunOutcome:
    fields: dict[str, Any] = {"job_id": "job-1", "state": state, "settled": False, "now": NOW}
    fields.update(kwargs)
    return RunOutcome(**fields)


class TestValidation:
    def test_a_bare_expectation_is_valid(self) -> None:
        Expectation().validate()

    def test_negative_episode_counts_are_rejected(self) -> None:
        with pytest.raises(InvalidExpectation):
            Expectation(expected_episodes=-1).validate()

    def test_a_valid_fraction_above_one_is_rejected(self) -> None:
        with pytest.raises(InvalidExpectation):
            Expectation(expected_valid_fraction=1.5).validate()

    def test_a_non_positive_duration_is_rejected(self) -> None:
        with pytest.raises(InvalidExpectation):
            Expectation(max_duration_seconds=0).validate()

    def test_a_naive_deadline_is_rejected(self) -> None:
        with pytest.raises(InvalidExpectation):
            Expectation(deadline_at=datetime(2026, 9, 29)).validate()

    def test_declared_fields_lists_only_what_was_set(self) -> None:
        assert Expectation(expected_episodes=3).declared_fields() == ("expected_episodes",)
        assert Expectation().declared_fields() == ()


class TestNoExpectation:
    def test_an_empty_expectation_stays_pending(self) -> None:
        assert evaluate(Expectation(), _settled(3, 3)) == (OUTCOME_PENDING, [])

    def test_an_unsettled_run_is_pending_not_breached(self) -> None:
        outcome, signals = evaluate(Expectation(expected_episodes=200), _unsettled())
        assert outcome == OUTCOME_PENDING
        assert signals == []


class TestEpisodeCount:
    def test_too_few_valid_episodes_breaches(self) -> None:
        outcome, signals = evaluate(Expectation(expected_episodes=200), _settled(120, 118))
        assert outcome == OUTCOME_SHORT
        assert len(signals) == 1
        signal = signals[0]
        assert signal.label == "CONTRACT_BREACH"
        assert signal.severity.value == "critical"
        assert signal.evidence["shortfall"] == 82
        assert signal.evidence["expected_episodes"] == 200

    def test_enough_episodes_meets_the_contract(self) -> None:
        assert evaluate(Expectation(expected_episodes=100), _settled(140, 140))[0] == OUTCOME_MET

    def test_exactly_the_expected_count_meets_the_contract(self) -> None:
        assert evaluate(Expectation(expected_episodes=100), _settled(100, 100))[0] == OUTCOME_MET

    def test_produced_counts_but_valid_decides(self) -> None:
        outcome, _ = evaluate(Expectation(expected_episodes=100), _settled(120, 20))
        assert outcome == OUTCOME_SHORT

    def test_impossible_counts_are_clamped(self) -> None:
        outcome, signals = evaluate(Expectation(expected_episodes=10), _settled(5, 99))
        assert outcome == OUTCOME_SHORT
        assert signals[0].evidence["valid_episodes"] == 5


class TestValidFraction:
    def test_a_degraded_run_breaches(self) -> None:
        outcome, signals = evaluate(Expectation(expected_valid_fraction=0.9), _settled(100, 40))
        assert outcome == OUTCOME_DEGRADED
        assert signals[0].label == "PARTIAL_SUCCESS"
        assert signals[0].evidence["observed_valid_fraction"] == 0.4

    def test_a_clean_run_meets_the_fraction(self) -> None:
        assert evaluate(Expectation(expected_valid_fraction=0.9), _settled(100, 95))[0] == (
            OUTCOME_MET
        )

    def test_an_empty_run_is_not_a_fraction_breach(self) -> None:
        assert evaluate(Expectation(expected_valid_fraction=0.9), _settled(0, 0))[0] == OUTCOME_MET


class TestTiming:
    def test_a_passed_deadline_on_an_unfinished_run_misses(self) -> None:
        expectation = Expectation(deadline_at=NOW - timedelta(minutes=5))
        outcome, signals = evaluate(expectation, _unsettled(state="running"))
        assert outcome == OUTCOME_MISSED
        assert signals[0].label == "TIME_MISSED"
        assert signals[0].evidence["overdue_seconds"] == 300.0

    def test_a_future_deadline_is_not_yet_missed(self) -> None:
        expectation = Expectation(deadline_at=NOW + timedelta(minutes=5))
        assert evaluate(expectation, _unsettled(state="running")) == (OUTCOME_PENDING, [])

    def test_an_exceeded_duration_misses(self) -> None:
        expectation = Expectation(max_duration_seconds=60.0)
        outcome, signals = evaluate(expectation, _settled(3, 3, duration_seconds=600.0))
        assert outcome == OUTCOME_MISSED
        assert signals[0].evidence["duration_seconds"] == 600.0

    def test_a_run_within_its_budget_meets_the_contract(self) -> None:
        expectation = Expectation(max_duration_seconds=600.0)
        assert evaluate(expectation, _settled(3, 3, duration_seconds=60.0))[0] == OUTCOME_MET

    def test_a_settled_run_past_its_deadline_is_not_reported_as_running_late(self) -> None:
        expectation = Expectation(deadline_at=NOW - timedelta(minutes=5))
        assert evaluate(expectation, _settled(3, 3))[0] == OUTCOME_MET


class TestFirstBreachWins:
    def test_a_short_and_late_run_reports_once(self) -> None:
        expectation = Expectation(expected_episodes=200, deadline_at=NOW - timedelta(minutes=5))
        outcome, signals = evaluate(expectation, _settled(10, 10))
        assert outcome == OUTCOME_SHORT
        assert len(signals) == 1

    def test_the_reported_breach_names_its_own_label(self) -> None:
        expectation = Expectation(expected_episodes=200, expected_valid_fraction=0.9)
        _, signals = evaluate(expectation, _settled(10, 1))
        assert signals[0].label == "CONTRACT_BREACH"
        assert signals[0].evidence["shortfall"] == 199


class TestSerialisation:
    def test_round_trips_through_a_dict(self) -> None:
        original = Expectation(
            expected_episodes=10,
            expected_valid_fraction=0.8,
            max_duration_seconds=30.0,
            deadline_at=NOW,
        )
        restored = Expectation.from_dict(original.to_dict())
        assert restored == original

    def test_absent_fields_deserialise_to_none(self) -> None:
        assert Expectation.from_dict({}) == Expectation()

    def test_nulls_deserialise_to_none(self) -> None:
        payload = Expectation().to_dict()
        assert Expectation.from_dict(payload) == Expectation()
