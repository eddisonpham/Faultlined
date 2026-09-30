"""Unit tests for the chaos harness (B-016).

These are tests of the *harness*, not of detector accuracy: each one pins a
property the fault-injection campaign will rely on. The seams must be narrow
and honest (an injected window is really what the tick sees), the scoring must
measure latency from the moment detection was possible, and the replay clock
must actually move.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from data_engine.monitoring.baselines import Baseline
from data_engine.monitoring.chaos import (
    HEARTBEAT_INTERVAL_SECONDS,
    ChaosMonitor,
    FaultWindow,
    clean_window,
    evaluate_sequence,
    evaluate_window,
    heartbeat_records,
    queue_backlog_window,
    worker_lost_window,
)
from data_engine.monitoring.detectors import DEFAULT_CONFIG as DETECTOR_CONFIG
from data_engine.monitoring.service import MonitorConfig
from data_engine.monitoring.signals import Label

T0 = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
WINDOW_SECONDS = 60.0


class ChaosCatalog:
    """The monitor's eight catalog seams, in memory.

    The platform side (`monitoring_snapshot`) refuses to answer: every window
    this file runs injects its own snapshot, and a silent fallback to a "real"
    probe would mean the harness was scored against nothing. Monitor-side state
    (baselines, incidents) behaves like the repository's, so hold/freeze and
    dedup run for real across ticks.
    """

    def __init__(self) -> None:
        self.baseline_rows: list[dict[str, Any]] = []
        self.incidents: list[dict[str, Any]] = []
        self.contract_outcomes: list[dict[str, Any]] = []

    def monitoring_snapshot(self, now: datetime) -> dict[str, Any]:
        raise RuntimeError("chaos windows inject their snapshot; the real probe must not run")

    def load_baselines(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.baseline_rows]

    def save_baselines(self, rows: list[dict[str, Any]]) -> None:
        self.baseline_rows = [dict(row) for row in rows]

    def upsert_incident(self, **kwargs: Any) -> dict[str, Any]:
        # Upsert semantics, like the repository: a bump updates the row in
        # place (occurrence count and last-seen move) rather than appending.
        for index, existing in enumerate(self.incidents):
            if existing["id"] == kwargs["incident_id"]:
                bumped = {
                    **existing,
                    **kwargs,
                    "occurrence_count": existing["occurrence_count"] + 1,
                    "last_seen": kwargs["seen_at"],
                }
                self.incidents[index] = bumped
                return bumped
        row = {
            "id": kwargs["incident_id"],
            "status": "open",
            "occurrence_count": 1,
            "last_seen": kwargs["seen_at"],
            **kwargs,
        }
        self.incidents.append(row)
        return row

    def unresolved_incidents(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.incidents]

    def incidents_opened_since(self, since: datetime) -> int:
        return sum(1 for row in self.incidents if row["last_seen"] >= since)

    def pending_contract_outcomes(self) -> list[dict[str, Any]]:
        return []

    def set_contract_outcome(self, job_id: str, outcome: str, observed: dict[str, Any]) -> None:
        self.contract_outcomes.append({"job_id": job_id, "outcome": outcome, "observed": observed})


def _monitor() -> tuple[ChaosMonitor, ChaosCatalog]:
    catalog = ChaosCatalog()
    config = MonitorConfig(tick_seconds=WINDOW_SECONDS)
    monitor = ChaosMonitor(catalog, metrics_path=None, config=config, now=T0)
    return monitor, catalog


def _warm(monitor: ChaosMonitor, feature: str, value: float, *, n: int = 25) -> None:
    """Warm one baseline the way production would: persisted, then loaded.

    The service reloads its book from the catalog on every reachable tick, so
    seeding the in-memory book directly would be discarded before any rule
    could read it.
    """
    monitor._catalog.baseline_rows.append(  # type: ignore[union-attr]
        Baseline(feature=feature, scope="", samples=(value,) * n, center=value).to_row()
    )


class TestWindowBuilders:
    def test_worker_lost_window_clears_the_strict_staleness_bound(self) -> None:
        # The detector fires on age > threshold, not >=. At detectable_at the
        # last beat must be older than the threshold with margin, and at onset
        # the age must read exactly one threshold so an early tick is silent
        # rather than borderline.
        window = worker_lost_window(onset=T0, window_seconds=WINDOW_SECONDS)
        beat = datetime.fromisoformat(str(window.records[0]["timestamp"]))
        assert beat == window.onset - timedelta(seconds=2 * WINDOW_SECONDS + 60.0)
        assert (window.detectable_at - beat).total_seconds() > (
            DETECTOR_CONFIG.heartbeat_stale_seconds
        )
        assert (window.onset - beat).total_seconds() == DETECTOR_CONFIG.heartbeat_stale_seconds

    def test_worker_lost_window_holds_work_outstanding(self) -> None:
        # A lost worker only matters while the queue wants one; an idle system
        # with a stale heartbeat must not be scored as a fault.
        window = worker_lost_window(onset=T0, window_seconds=WINDOW_SECONDS)
        assert window.snapshot is not None
        depth = window.snapshot["queue_depth"]
        assert depth["queued"] or depth["running"]

    def test_heartbeat_records_are_cadenced_and_end_at_the_request(self) -> None:
        end = T0 + timedelta(minutes=5)
        records = heartbeat_records(count=4, end_at=end)
        stamps = [datetime.fromisoformat(str(record["timestamp"])) for record in records]
        assert stamps[0] == end
        assert stamps[-1] == end - timedelta(seconds=3 * HEARTBEAT_INTERVAL_SECONDS)
        assert all(record["value"] == 0.0 for record in records)

    def test_backlog_window_carries_a_live_worker(self) -> None:
        # A backlog with no heartbeats is two faults, not one: the window would
        # fire WORKER_LOST and the harness would score it blind. The worker is
        # alive in this fault.
        window = queue_backlog_window(onset=T0, window_seconds=WINDOW_SECONDS)
        assert window.records, "backlog window must carry fresh heartbeats"
        assert window.label == ""  # no dedicated rule owns a backlog

    def test_clean_window_is_unlabeled_and_progressing(self) -> None:
        window = clean_window(onset=T0, window_seconds=WINDOW_SECONDS)
        assert window.label == ""
        names = [record["name"] for record in window.records]
        assert "episodes_ingested_total" in names


class TestEvaluateWindow:
    def test_worker_lost_is_detected_and_persisted_at_the_detectable_instant(self) -> None:
        monitor, catalog = _monitor()
        window = worker_lost_window(onset=T0, window_seconds=WINDOW_SECONDS)
        outcome = evaluate_window(monitor, window)
        assert outcome.detected
        assert Label.WORKER_LOST.value in outcome.fired_labels
        assert outcome.latency_seconds == 0.0
        assert outcome.blind is False
        assert outcome.incidents_touched == 1
        assert catalog.incidents[0]["label"] == Label.WORKER_LOST.value

    def test_detection_at_onset_is_silent(self) -> None:
        # Scoring at onset would credit detection the pipeline cannot have:
        # the staleness clock has not run out yet, so the tick must say nothing.
        monitor, _ = _monitor()
        window = worker_lost_window(onset=T0, window_seconds=WINDOW_SECONDS)
        outcome = evaluate_window(monitor, window, tick_at=window.onset)
        assert not outcome.detected
        assert Label.WORKER_LOST.value not in outcome.fired_labels

    def test_late_tick_scores_latency_from_detectable_at(self) -> None:
        monitor, _ = _monitor()
        window = worker_lost_window(onset=T0, window_seconds=WINDOW_SECONDS)
        late = window.detectable_at + timedelta(seconds=120)
        outcome = evaluate_window(monitor, window, tick_at=late)
        assert outcome.detected
        assert outcome.latency_seconds == pytest.approx(120.0)
        assert outcome.observed_at == late

    def test_backlog_is_not_scored_as_a_lost_worker_or_blind(self) -> None:
        # The trap this test pins: a backlog window without heartbeats would
        # double-fire as WORKER_LOST and flip the blind flag - mislabeled truth.
        monitor, _ = _monitor()
        window = queue_backlog_window(onset=T0, window_seconds=WINDOW_SECONDS)
        outcome = evaluate_window(monitor, window)
        assert outcome.blind is False
        assert Label.WORKER_LOST.value not in outcome.fired_labels
        assert outcome.detected is False  # no dedicated rule owns a cold backlog

    def test_warm_backlog_shift_surfaces_as_metric_shift(self) -> None:
        # A backlog *is* detectable once the queue baselines are warm - by the
        # residual consensus rule, which is the designed path, not a new rule.
        monitor, _ = _monitor()
        _warm(monitor, "queue_oldest_age_seconds", 1.0)
        _warm(monitor, "queue_depth_queued", 1.0)
        window = queue_backlog_window(onset=T0, window_seconds=WINDOW_SECONDS)
        outcome = evaluate_window(monitor, window)
        assert Label.METRIC_SHIFT.value in outcome.fired_labels
        assert outcome.blind is False
        assert outcome.incidents_touched == 1

    def test_clean_window_stays_silent(self) -> None:
        monitor, _ = _monitor()
        outcome = evaluate_window(monitor, clean_window(onset=T0, window_seconds=WINDOW_SECONDS))
        assert outcome.fired_labels == []
        assert outcome.detected is False
        assert outcome.blind is False
        assert outcome.incidents_touched == 0
        assert outcome.latency_seconds is None

    def test_the_only_overrides_are_the_two_documented_seams(self) -> None:
        # Anything beyond `_records`/`_probe` (plus construction and injection)
        # would make "chaos result" mean "a different implementation ran".
        authored = {name for name in ChaosMonitor.__dict__ if not name.startswith("__")}
        assert authored == {"inject", "_records", "_probe"}

    def test_uninjected_snapshot_falls_through_to_the_real_probe(self) -> None:
        # A window with snapshot=None must exercise the service's own probe;
        # here that probe refuses, so the tick must report unreachable and
        # blind - not quietly carry the previous window's snapshot.
        monitor, _ = _monitor()
        monitor.inject(FaultWindow(label="", onset=T0, detectable_at=T0, records=[]))
        monitor._chaos_snapshot = None
        outcome = evaluate_window(
            monitor, FaultWindow(label="", onset=T0, detectable_at=T0, records=[])
        )
        assert outcome.blind is True
        assert Label.DATABASE_UNREACHABLE.value in outcome.fired_labels


class TestEvaluateSequence:
    def test_sequence_runs_in_replay_order_and_keeps_the_book(self) -> None:
        monitor, catalog = _monitor()
        windows = [
            clean_window(onset=T0, window_seconds=WINDOW_SECONDS),
            worker_lost_window(
                onset=T0 + timedelta(seconds=WINDOW_SECONDS), window_seconds=WINDOW_SECONDS
            ),
            clean_window(
                onset=T0 + timedelta(seconds=2 * WINDOW_SECONDS), window_seconds=WINDOW_SECONDS
            ),
        ]
        outcomes = evaluate_sequence(monitor, windows)
        assert [outcome.observed_at for outcome in outcomes] == [
            window.detectable_at for window in windows
        ]
        assert outcomes[1].detected
        assert not outcomes[0].detected and not outcomes[2].detected
        assert len(catalog.incidents) == 1
        assert monitor._book is not None

    def test_sequence_rejects_time_running_backwards(self) -> None:
        monitor, _ = _monitor()
        late = worker_lost_window(onset=T0, window_seconds=WINDOW_SECONDS)
        early = clean_window(onset=T0 - timedelta(hours=1), window_seconds=WINDOW_SECONDS)
        with pytest.raises(ValueError, match="ordered by detectable_at"):
            evaluate_sequence(monitor, [late, early])

    def test_sustained_fault_holds_its_baseline(self) -> None:
        # The freeze rule: while a fault stays in breach, its baseline must not
        # absorb the fault as the new normal, and the second tick bumps the
        # same incident instead of opening a second one.
        monitor, catalog = _monitor()
        _warm(monitor, "queue_oldest_age_seconds", 1.0)
        _warm(monitor, "queue_depth_queued", 1.0)
        first, second = evaluate_sequence(
            monitor,
            [
                queue_backlog_window(onset=T0, window_seconds=WINDOW_SECONDS),
                queue_backlog_window(
                    onset=T0 + timedelta(seconds=WINDOW_SECONDS), window_seconds=WINDOW_SECONDS
                ),
            ],
        )
        assert Label.METRIC_SHIFT.value in first.fired_labels
        assert Label.METRIC_SHIFT.value in second.fired_labels
        assert len(catalog.incidents) == 1
        book = monitor._book
        assert book is not None
        held = book.get("queue_oldest_age_seconds")
        assert held is not None and held.breached
