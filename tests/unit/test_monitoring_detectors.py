"""Unit tests for the detector rules (ADR 0020)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from data_engine.monitoring.baselines import MIN_OBSERVATIONS, BaselineBook
from data_engine.monitoring.detectors import (
    DETECTORS,
    GIB,
    DetectorConfig,
    DetectorContext,
    detect,
    observations_for,
)
from data_engine.monitoring.features import (
    FEATURE_NAMES,
    FeatureInputs,
    ResourceSample,
    build_features,
)
from data_engine.monitoring.signals import Label, NotifyClass, Severity

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
HEALTHY: dict[str, Any] = {
    "queue_depth": {"queued": 0, "running": 0, "failed": 0},
    "quarantine_rate": 0.02,
    "verdict_rates": {"smooth": 90, "moderate": 8, "jerky": 2},
    "movement_mean": 0.2,
    "jerk_mean": 0.02,
    "stall_mean": 0.05,
    "frame_count_median": 200.0,
    "oldest_queued_age_seconds": 1.0,
    "max_episode_timestamp_gap_seconds": 0.04,
    "timestamp_gap_scope": "episode-1",
    "timestamp_gap_source": "source-1",
}
HEALTHY_RESOURCES = ResourceSample(
    cpu_percent=12.0,
    memory_used_bytes=4e9,
    memory_available_bytes=12e9,
    disk_free_bytes=200 * GIB,
)
STALE_HEARTBEAT = {
    "name": "workers_heartbeat_age_seconds",
    "value": 0.0,
    "unit": "seconds",
    "labels": {"worker_state": "idle"},
    "timestamp": "2026-09-29T11:55:00+00:00",
    "source": "runtime",
}


def _context(
    snapshot: dict[str, Any] | None = None,
    *,
    records: list[dict[str, Any]] | None = None,
    resources: ResourceSample | None = HEALTHY_RESOURCES,
    baselines: BaselineBook | None = None,
    running: list[dict[str, Any]] | None = None,
    catalog_reachable: bool = True,
    config: DetectorConfig | None = None,
) -> DetectorContext:
    merged = dict(HEALTHY)
    merged.update(snapshot or {})
    features = build_features(
        FeatureInputs(
            now=NOW,
            records=records or [],
            snapshot=merged,
            resources=resources,
            catalog_reachable=catalog_reachable,
        )
    )
    return DetectorContext(
        now=NOW,
        features=features,
        baselines=baselines or BaselineBook(),
        running=running or [],
        config=config or DetectorConfig(),
    )


def _labels(context: DetectorContext) -> set[str]:
    return {signal.label for signal in detect(context)}


def _warm(
    book: BaselineBook, feature: str, value: float, *, n: int = MIN_OBSERVATIONS + 10
) -> None:
    for _ in range(n):
        book.observe(feature, value)


class TestRegistry:
    def test_every_rule_is_registered_and_callable(self) -> None:
        assert len(DETECTORS) == 11
        for name, rule in DETECTORS:
            assert name and callable(rule)

    def test_health_produces_no_incidents(self) -> None:
        assert detect(_context()) == []


class TestAbsoluteRules:
    def test_database_unreachable_when_the_probe_fails(self) -> None:
        signals = detect(_context(catalog_reachable=False))
        assert Label.DATABASE_UNREACHABLE.value in {s.label for s in signals}
        assert all(s.severity == Severity.CRITICAL for s in signals)

    def test_worker_lost_needs_a_stale_heartbeat_and_outstanding_work(self) -> None:
        assert Label.WORKER_LOST.value not in _labels(_context(records=[STALE_HEARTBEAT]))
        busy = _context(
            snapshot={"queue_depth": {"queued": 2}},
            records=[STALE_HEARTBEAT],
        )
        assert Label.WORKER_LOST.value in _labels(busy)

    def test_worker_lost_does_not_page_an_idle_platform(self) -> None:
        assert Label.WORKER_LOST.value not in _labels(_context(records=[STALE_HEARTBEAT]))

    def test_never_seen_heartbeat_is_reported_distinctly(self) -> None:
        signals = detect(_context(snapshot={"queue_depth": {"running": 1}}))
        lost = [s for s in signals if s.label == Label.WORKER_LOST.value]
        assert lost and lost[0].evidence["heartbeat_age_seconds"] is None

    def test_run_stalled_beyond_the_threshold_with_no_progress(self) -> None:
        context = _context(running=[{"id": "job-1", "job_type": "ingest", "age_seconds": 2000.0}])
        labels = _labels(context)
        assert Label.RUN_STALLED.value in labels

    def test_run_stalled_is_silent_while_episodes_land(self) -> None:
        records = [
            {
                "name": "episodes_ingested_total",
                "value": 1.0,
                "labels": {},
                "timestamp": "2026-09-29T12:00:00+00:00",
                "source": "runtime",
            }
        ]
        context = _context(
            running=[{"id": "job-1", "job_type": "ingest", "age_seconds": 2000.0}], records=records
        )
        assert Label.RUN_STALLED.value not in _labels(context)

    def test_run_stalled_is_silent_below_the_threshold(self) -> None:
        context = _context(running=[{"id": "job-1", "job_type": "ingest", "age_seconds": 30.0}])
        assert Label.RUN_STALLED.value not in _labels(context)

    def test_two_stalled_runs_are_two_incidents(self) -> None:
        context = _context(
            running=[
                {"id": "job-1", "job_type": "ingest", "age_seconds": 2000.0},
                {"id": "job-2", "job_type": "validate", "age_seconds": 3000.0},
            ]
        )
        stalled = [s for s in detect(context) if s.label == Label.RUN_STALLED.value]
        assert {s.scope for s in stalled} == {"job-1", "job-2"}

    def test_disk_pressure_below_the_floor(self) -> None:
        context = _context(
            resources=ResourceSample(cpu_percent=5.0, disk_free_bytes=500 * 1024 * 1024)
        )
        signals = detect(context)
        assert Label.DISK_PRESSURE.value in {s.label for s in signals}
        assert (
            next(s for s in signals if s.label == Label.DISK_PRESSURE.value).notify_class
            == NotifyClass.NOTIFY
        )

    def test_disk_pressure_is_quiet_with_plenty_of_space(self) -> None:
        assert Label.DISK_PRESSURE.value not in _labels(_context())

    def test_clock_drift_on_a_gap_inside_one_episode(self) -> None:
        context = _context(snapshot={"max_episode_timestamp_gap_seconds": 9.0})
        signals = [s for s in detect(context) if s.label == Label.CLOCK_DRIFT.value]
        assert signals
        assert signals[0].scope == "episode-1"
        assert signals[0].evidence["source"] == "source-1"

    def test_clock_drift_abstains_without_timestamps(self) -> None:
        context = _context(snapshot={"max_episode_timestamp_gap_seconds": None})
        context = _context(
            snapshot={
                "max_episode_timestamp_gap_seconds": None,
            }
        )
        assert Label.CLOCK_DRIFT.value not in _labels(context)

    def test_repeated_read_failure_is_scoped_by_reason_code(self) -> None:
        records = [
            {
                "name": "jobs_failures_total",
                "value": 1.0,
                "labels": {"reason_code": "READ_FAILED"},
                "timestamp": "2026-09-29T12:00:00+00:00",
                "source": "runtime",
            }
            for _ in range(3)
        ]
        signals = [
            s
            for s in detect(_context(records=records))
            if s.label == Label.REPEATED_READ_FAILURE.value
        ]
        assert signals and signals[0].scope == "READ_FAILED"
        assert signals[0].evidence["failure_count"] == 3.0

    def test_a_single_failure_is_not_a_storm(self) -> None:
        records = [
            {
                "name": "jobs_failures_total",
                "value": 1.0,
                "labels": {"reason_code": "READ_FAILED"},
                "timestamp": "2026-09-29T12:00:00+00:00",
                "source": "runtime",
            }
        ]
        assert Label.REPEATED_READ_FAILURE.value not in _labels(_context(records=records))

    def test_resource_degraded_on_host_pressure(self) -> None:
        context = _context(
            resources=ResourceSample(
                cpu_percent=98.0, memory_used_bytes=9e9, memory_available_bytes=1e8
            )
        )
        assert Label.RESOURCE_DEGRADED.value in _labels(context)


class TestBaselineRules:
    def test_quarantine_absolute_bound_works_on_a_cold_scope(self) -> None:
        context = _context(snapshot={"quarantine_rate": 0.9})
        signals = [s for s in detect(context) if s.label == Label.QUARANTINE_RATE_HIGH.value]
        assert signals and signals[0].evidence["basis"] == "absolute"

    def test_quarantine_baseline_bound_works_when_warm(self) -> None:
        book = BaselineBook()
        _warm(book, "quarantine_rate", 0.02)
        context = _context(snapshot={"quarantine_rate": 0.30}, baselines=book)
        signals = [s for s in detect(context) if s.label == Label.QUARANTINE_RATE_HIGH.value]
        assert signals and signals[0].evidence["basis"] == "baseline"
        assert "baseline_median" in signals[0].evidence

    def test_quarantine_abstains_on_a_cold_scope_below_the_absolute_bound(self) -> None:
        context = _context(snapshot={"quarantine_rate": 0.10})
        assert Label.QUARANTINE_RATE_HIGH.value not in _labels(context)

    def test_quarantine_cites_no_baseline_it_does_not_have(self) -> None:
        context = _context(snapshot={"quarantine_rate": 0.9})
        signal = next(s for s in detect(context) if s.label == Label.QUARANTINE_RATE_HIGH.value)
        assert signal.evidence["baseline_median"] is None

    def test_quality_shift_needs_a_warm_baseline(self) -> None:
        cold = _context(snapshot={"verdict_rates": {"smooth": 0, "jerky": 100}})
        assert Label.QUALITY_SHIFT.value not in _labels(cold)

        book = BaselineBook()
        _warm(book, "verdict_jerky_rate", 0.05)
        warm = _context(snapshot={"verdict_rates": {"smooth": 0, "jerky": 100}}, baselines=book)
        assert Label.QUALITY_SHIFT.value in _labels(warm)

    def test_quality_shift_reports_the_strongest_signal_only(self) -> None:
        book = BaselineBook()
        _warm(book, "verdict_jerky_rate", 0.05)
        _warm(book, "jerk_mean", 0.01)
        context = _context(
            snapshot={"verdict_rates": {"smooth": 0, "jerky": 100}, "jerk_mean": 0.9},
            baselines=book,
        )
        shifts = [s for s in detect(context) if s.label == Label.QUALITY_SHIFT.value]
        assert len(shifts) == 1

    def test_frame_count_collapse_is_relative_to_the_platform(self) -> None:
        book = BaselineBook()
        _warm(book, "frame_count_median", 200.0)
        context = _context(snapshot={"frame_count_median": 30.0}, baselines=book)
        signals = [s for s in detect(context) if s.label == Label.FRAME_COUNT_COLLAPSE.value]
        assert signals and signals[0].evidence["ratio_to_center"] == pytest.approx(0.15)

    def test_frame_count_collapse_abstains_when_cold(self) -> None:
        context = _context(snapshot={"frame_count_median": 30.0})
        assert Label.FRAME_COUNT_COLLAPSE.value not in _labels(context)


class TestMetricShiftConsensus:
    def _steady(self) -> BaselineBook:
        book = BaselineBook()
        for _ in range(MIN_OBSERVATIONS + 10):
            book.observe("queue_oldest_age_seconds", 5.0)
            book.observe("queue_depth_queued", 1.0)
        return book

    def test_one_shifted_feature_is_not_an_incident(self) -> None:
        context = _context(
            snapshot={"queue_depth": {"queued": 1}, "oldest_queued_age_seconds": 900.0},
            baselines=self._steady(),
        )
        assert Label.METRIC_SHIFT.value not in _labels(context)

    def test_several_shifted_features_are_one_incident(self) -> None:
        context = _context(
            snapshot={"queue_depth": {"queued": 9}, "oldest_queued_age_seconds": 900.0},
            baselines=self._steady(),
        )
        signals = [s for s in detect(context) if s.label == Label.METRIC_SHIFT.value]
        assert len(signals) == 1
        assert len(signals[0].evidence["shifted"]) >= 2
        assert signals[0].notify_class == NotifyClass.QUEUE

    def test_shifted_features_are_ordered_by_magnitude(self) -> None:
        context = _context(
            snapshot={"queue_depth": {"queued": 9}, "oldest_queued_age_seconds": 900.0},
            baselines=self._steady(),
        )
        signal = next(s for s in detect(context) if s.label == Label.METRIC_SHIFT.value)
        z_scores = [abs(float(item["robust_z"])) for item in signal.evidence["shifted"]]
        assert z_scores == sorted(z_scores, reverse=True)

    def test_a_downward_shift_counts_for_low_watched_features(self) -> None:
        book = BaselineBook()
        for _ in range(MIN_OBSERVATIONS + 10):
            book.observe("episodes_per_second", 5.0)
        context = _context(
            snapshot={"episodes_per_second": 0.0, "queue_depth_queued": 40.0}, baselines=book
        )
        assert Label.METRIC_SHIFT.value not in _labels(context)

    def test_consensus_threshold_is_configurable(self) -> None:
        book = self._steady()
        context = _context(
            snapshot={"queue_depth": {"queued": 9}, "oldest_queued_age_seconds": 900.0},
            baselines=book,
            config=DetectorConfig(metric_shift_min_features=1),
        )
        signals = [s for s in detect(context) if s.label == Label.METRIC_SHIFT.value]
        assert signals and len(signals[0].evidence["shifted"]) >= 1


class TestObservations:
    def test_observations_follow_the_schema_order_and_are_stable(self) -> None:
        first = [
            name for name, _, _ in observations_for(_context(records=[STALE_HEARTBEAT]).features)
        ]
        second = [
            name for name, _, _ in observations_for(_context(records=[STALE_HEARTBEAT]).features)
        ]
        assert first == second
        positions = [FEATURE_NAMES.index(name) for name in first]
        assert positions == sorted(positions)

    def test_every_observation_has_a_global_scope_except_scoped_features(self) -> None:
        triples = observations_for(_context(records=[STALE_HEARTBEAT]).features)
        assert all(scope == "" for _, _, scope in triples)

    def test_scoped_run_times_are_observed_per_job_type(self) -> None:
        records = [
            {
                "name": "jobs_run_time_seconds",
                "value": 5.0,
                "labels": {"state": "succeeded", "job_type": "ingest"},
                "timestamp": "2026-09-29T12:00:00+00:00",
                "source": "runtime",
            }
        ]
        context = _context(records=records)
        assert ("run_time_p95_seconds", 5.0, "ingest") in observations_for(context.features)

    def test_identifiers_are_never_observed(self) -> None:
        names = {name for name, _, _ in observations_for(_context().features)}
        assert "timestamp_gap_scope" not in names
        assert "timestamp_gap_source" not in names
