"""Unit tests for control limits: EWMA centre, median/MAD spread, and the freeze.

Each behaviour here exists because its absence produces a specific, observable
failure — a baseline that adapts to a fault, a cold scope that guesses, or a
stale hold that pins an incident open forever.
"""

from __future__ import annotations

import pytest

from data_engine.monitoring.baselines import (
    EWMA_ALPHA,
    MAX_HOLD_SECONDS,
    MIN_OBSERVATIONS,
    SAMPLE_WINDOW,
    Baseline,
    BaselineBook,
)


def _warm(book: BaselineBook, feature: str, value: float, *, scope: str = "", n: int = 30) -> None:
    for _ in range(n):
        book.observe(feature, value, scope=scope)


class TestCentre:
    def test_first_observation_seeds_the_centre(self) -> None:
        book = BaselineBook()
        assert book.observe("x", 5.0).center == 5.0

    def test_centre_is_an_ewma(self) -> None:
        book = BaselineBook()
        book.observe("x", 10.0)
        expected = 10.0 + EWMA_ALPHA * (20.0 - 10.0)
        assert book.observe("x", 20.0).center == pytest.approx(expected)

    def test_centre_tracks_a_level_shift(self) -> None:
        book = BaselineBook()
        _warm(book, "x", 1.0)
        assert book.get("x") is not None
        before = float(book.get("x").center)  # type: ignore[union-attr]
        for _ in range(40):
            book.observe("x", 9.0)
        after = float(book.get("x").center)  # type: ignore[union-attr]
        assert after > before + 5


class TestRobustSpread:
    def test_median_and_mad_on_a_known_sample(self) -> None:
        baseline = Baseline("x", samples=(1.0, 2.0, 3.0, 4.0, 5.0))
        assert baseline.median == 3.0
        assert baseline.mad == 1.0

    def test_mad_is_zero_for_a_constant_scope(self) -> None:
        assert Baseline("x", samples=(2.0, 2.0, 2.0)).mad == 0.0

    def test_sigma_has_a_scale_aware_floor(self) -> None:
        # A perfectly stable scope has MAD 0; without a floor the z-score would be
        # infinite and any tiny change would read as an infinite deviation.
        constant = Baseline("x", samples=(100.0,) * 10)
        assert constant.sigma > 0.0
        assert constant.sigma == pytest.approx(0.05 * 100.0)

    def test_one_spike_does_not_widen_the_limits(self) -> None:
        # This is the whole reason for median/MAD: a mean/std baseline would
        # absorb the outlier and stop catching it next time.
        stable = Baseline("x", samples=(10.0,) * 25 + (10_000.0,))
        assert stable.median == 10.0
        assert stable.mad == 0.0

    def test_sigma_converts_mad_to_a_sigma_equivalent(self) -> None:
        assert Baseline("x", samples=(1.0, 2.0, 3.0, 4.0, 5.0)).sigma == pytest.approx(1.4826)


class TestColdStart:
    def test_a_cold_scope_is_not_warm(self) -> None:
        book = BaselineBook()
        _warm(book, "x", 1.0, n=MIN_OBSERVATIONS - 1)
        assert book.get("x") is not None
        assert not book.get("x").warm()  # type: ignore[union-attr]

    def test_warm_at_the_minimum_observation_count(self) -> None:
        book = BaselineBook()
        _warm(book, "x", 1.0, n=MIN_OBSERVATIONS)
        assert book.get("x") is not None
        assert book.get("x").warm()  # type: ignore[union-attr]

    def test_a_cold_scope_reports_zero_z(self) -> None:
        # Absence of evidence is not evidence of normality, so the value is 0.0
        # and callers must check warm() before trusting it.
        book = BaselineBook()
        book.observe("x", 1.0)
        baseline = book.get("x")
        assert baseline is not None
        assert baseline.robust_z(1000.0) == 0.0


class TestFreezeDuringBreach:
    def test_a_held_scope_does_not_absorb(self) -> None:
        book = BaselineBook()
        _warm(book, "x", 1.0)
        before = book.get("x")
        assert before is not None
        book.hold("x", now=100.0)
        after = book.observe("x", 9999.0)
        assert after.samples == before.samples
        assert after.center == before.center

    def test_release_resumes_absorption(self) -> None:
        book = BaselineBook()
        _warm(book, "x", 1.0)
        book.hold("x", now=100.0)
        book.release("x")
        assert book.observe("x", 2.0).samples[-1] == 2.0

    def test_hold_is_idempotent_and_keeps_the_first_timestamp(self) -> None:
        book = BaselineBook()
        _warm(book, "x", 1.0)
        book.hold("x", now=100.0)
        book.hold("x", now=500.0)
        baseline = book.get("x")
        assert baseline is not None
        assert baseline.held_since == 100.0
        assert baseline.breached

    def test_release_expired_frees_a_stuck_hold(self) -> None:
        # Without this, a crash between the breach and the tick that would have
        # released it pins the incident open indefinitely.
        book = BaselineBook()
        _warm(book, "x", 1.0)
        book.hold("x", now=0.0)
        assert book.release_expired(now=MAX_HOLD_SECONDS - 1) == []
        assert book.release_expired(now=MAX_HOLD_SECONDS) == [("x", "")]
        assert book.get("x") is not None
        assert not book.get("x").breached  # type: ignore[union-attr]

    def test_release_on_an_unknown_scope_is_harmless(self) -> None:
        assert not BaselineBook().release("nope").breached


class TestScopes:
    def test_scopes_are_independent(self) -> None:
        book = BaselineBook()
        _warm(book, "run_time", 10.0, scope="ingest")
        _warm(book, "run_time", 100.0, scope="validate")
        assert book.get("run_time", "ingest").median == 10.0  # type: ignore[union-attr]
        assert book.get("run_time", "validate").median == 100.0  # type: ignore[union-attr]

    def test_keys_are_sorted_for_reproducible_iteration(self) -> None:
        book = BaselineBook()
        book.observe("b", 1.0)
        book.observe("a", 1.0)
        assert book.keys() == [("a", ""), ("b", "")]

    def test_warm_scopes_lists_only_usable_keys(self) -> None:
        book = BaselineBook()
        book.observe("cold", 1.0)
        _warm(book, "hot", 1.0)
        assert book.warm_scopes() == [("hot", "")]

    def test_the_window_is_bounded(self) -> None:
        book = BaselineBook()
        for index in range(SAMPLE_WINDOW + 25):
            book.observe("x", float(index))
        baseline = book.get("x")
        assert baseline is not None
        assert baseline.observations == SAMPLE_WINDOW
        assert baseline.samples[-1] == float(SAMPLE_WINDOW + 24)


class TestRowRoundTrip:
    def test_a_baseline_survives_a_database_round_trip(self) -> None:
        book = BaselineBook()
        _warm(book, "x", 3.0, scope="ingest")
        book.hold("x", scope="ingest", now=42.0)
        original = book.get("x", "ingest")
        assert original is not None

        restored = BaselineBook.from_rows([original.to_row()])
        after = restored.get("x", "ingest")
        assert after == original
        assert after is not None and after.breached and after.held_since == 42.0

    def test_a_row_with_too_many_samples_is_trimmed(self) -> None:
        row = {
            "feature": "x",
            "scope": "",
            "samples": [1.0] * (SAMPLE_WINDOW + 10),
            "center": 1.0,
        }
        assert Baseline.from_row(row).observations == SAMPLE_WINDOW

    def test_an_oversized_evidence_blind_pass(self) -> None:
        # Only a warm scope may be compared; a cold one must not answer.
        assert BaselineBook().get("never-seen") is None
