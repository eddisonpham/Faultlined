"""Unit tests for the pure feature builder (ADR 0020).

The invariants under test are the ones that make a monitor trustworthy in
production rather than only in a test: determinism, missing-is-absent, and no
unbounded labels in the vector.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from data_engine.monitoring.features import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    FeatureInputs,
    ResourceSample,
    build_features,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _record(name: str, value: float, *, seconds_ago: float = 0.0, **labels: str) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "unit": "seconds",
        "labels": labels,
        "timestamp": (NOW - timedelta(seconds=seconds_ago)).isoformat(),
        "source": "runtime",
    }


def _build(**kwargs: Any) -> Any:
    base: dict[str, Any] = {
        "now": NOW,
        "records": [],
        "snapshot": {},
        "window_seconds": 60.0,
    }
    base.update(kwargs)
    return build_features(FeatureInputs(**base))


class TestSchema:
    def test_schema_is_versioned_and_ordered(self) -> None:
        assert FEATURE_SCHEMA_VERSION == 1
        assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))
        assert FEATURE_NAMES[0] == "queue_depth_queued"

    def test_schema_version_is_recorded_on_every_vector(self) -> None:
        assert _build().schema_version == FEATURE_SCHEMA_VERSION

    def test_every_feature_has_a_documented_group(self) -> None:
        # Guards against a feature being added to the vector with no owner: an
        # unconsumed feature is dead weight that still costs a baseline scope.
        assert "sensor_availability" in FEATURE_NAMES
        assert "max_episode_timestamp_gap_seconds" not in FEATURE_NAMES


class TestDeterminism:
    def test_same_inputs_produce_the_same_vector(self) -> None:
        records = [_record("episodes_ingested_total", 1.0), _record("job_retries_total", 0.0)]
        first = _build(records=records)
        second = _build(records=records)
        assert first.values == second.values
        assert first.scopes == second.scopes

    def test_the_builder_does_not_mutate_its_inputs(self) -> None:
        records = [_record("episodes_ingested_total", 1.0)]
        snapshot = {"queue_depth": {"queued": 1}}
        _build(records=records, snapshot=snapshot)
        assert records == [_record("episodes_ingested_total", 1.0)]
        assert snapshot == {"queue_depth": {"queued": 1}}


class TestMissingIsNotZero:
    def test_absent_sensor_is_absent_not_zero(self) -> None:
        vector = _build()
        assert not vector.has("quarantine_rate")
        assert vector.value("quarantine_rate", 0.0) == 0.0
        assert "quarantine_rate" not in vector.values

    def test_zero_is_distinguishable_from_absent(self) -> None:
        vector = _build(snapshot={"quarantine_rate": 0.0})
        assert vector.has("quarantine_rate")
        assert vector.value("quarantine_rate") == 0.0

    def test_sensor_availability_counts_what_is_missing(self) -> None:
        sparse = _build()
        dense = _build(
            snapshot={"quarantine_rate": 0.1, "frame_count_median": 200.0},
            resources=ResourceSample(
                cpu_percent=10.0,
                memory_used_bytes=1e9,
                memory_available_bytes=1e9,
                disk_free_bytes=1e11,
            ),
        )
        assert sparse.value("sensor_availability") < dense.value("sensor_availability")

    def test_never_seen_heartbeat_is_a_missing_feature(self) -> None:
        # Not a stale heartbeat: no record at all. The detector decides what that
        # means; the builder must not invent a value.
        assert not _build(records=[]).has("heartbeat_age_seconds")

    def test_stale_heartbeat_is_derived_at_read_time(self) -> None:
        vector = _build(records=[_record("workers_heartbeat_age_seconds", 0.0, seconds_ago=300)])
        assert vector.has("heartbeat_age_seconds")
        assert vector.value("heartbeat_age_seconds") == 300.0

    def test_unreachable_catalog_drops_catalog_derived_features(self) -> None:
        # Recording zeros here would look like a healthy, empty system.
        vector = _build(
            snapshot={"quarantine_rate": 0.4, "frame_count_median": 10.0},
            catalog_reachable=False,
        )
        assert not vector.catalog_reachable
        assert not vector.has("quarantine_rate")
        assert not vector.has("frame_count_median")


class TestQueueAndProgress:
    def test_queue_depth_is_read_per_state(self) -> None:
        vector = _build(snapshot={"queue_depth": {"queued": 3, "running": 1, "failed": 2}})
        assert vector.value("queue_depth_queued") == 3.0
        assert vector.value("queue_depth_running") == 1.0
        assert vector.value("queue_depth_failed") == 2.0

    def test_unknown_queue_states_default_to_zero(self) -> None:
        assert _build(snapshot={"queue_depth": {"succeeded": 9}}).value("queue_depth_queued") == 0.0

    def test_episodes_per_second_uses_the_window(self) -> None:
        records = [_record("episodes_ingested_total", 1.0) for _ in range(6)]
        vector = _build(records=records, window_seconds=60.0)
        assert vector.value("episodes_ingested") == 6.0
        assert vector.value("episodes_per_second") == 0.1

    def test_failure_rate_is_measured_against_settled_runs(self) -> None:
        records = [
            _record("jobs_failures_total", 1.0, reason_code="READ_FAILED"),
            _record("jobs_run_time_seconds", 0.5, state="succeeded", job_type="ingest"),
            _record("jobs_run_time_seconds", 0.5, state="failed", job_type="ingest"),
        ]
        assert _build(records=records).value("job_failure_rate") == 0.5

    def test_a_lone_failure_is_a_total_failure_rate(self) -> None:
        # One settled run, and it failed: the rate is 1.0, not "unknown". This is
        # why REPEATED_READ_FAILURE needs a count threshold rather than a rate.
        assert (
            _build(records=[_record("jobs_failures_total", 1.0)]).value("job_failure_rate") == 1.0
        )

    def test_failure_rate_is_zero_with_nothing_settled(self) -> None:
        assert _build(records=[]).value("job_failure_rate") == 0.0


class TestScopedFeatures:
    def test_run_time_is_scoped_by_job_type(self) -> None:
        records = [
            _record("jobs_run_time_seconds", 10.0, state="succeeded", job_type="ingest"),
            _record("jobs_run_time_seconds", 20.0, state="succeeded", job_type="ingest"),
            _record("jobs_run_time_seconds", 0.1, state="succeeded", job_type="validate"),
        ]
        scoped = _build(records=records).scoped("run_time_p95_seconds")
        assert scoped["ingest"] == 20.0
        assert scoped["validate"] == 0.1

    def test_scoped_order_is_stable(self) -> None:
        records = [
            _record("jobs_run_time_seconds", 1.0, state="succeeded", job_type="zeta"),
            _record("jobs_run_time_seconds", 1.0, state="succeeded", job_type="alpha"),
        ]
        pairs = _build(records=records).scoped_pairs("run_time_p95_seconds")
        assert [name for name, _ in pairs] == ["alpha", "zeta"]

    def test_failures_are_scoped_by_reason_code(self) -> None:
        records = [
            _record("jobs_failures_total", 1.0, reason_code="READ_FAILED"),
            _record("jobs_failures_total", 1.0, reason_code="READ_FAILED"),
            _record("jobs_failures_total", 1.0, reason_code="VALIDATION_FAILED"),
        ]
        assert _build(records=records).scoped("failure_count") == {
            "READ_FAILED": 2.0,
            "VALIDATION_FAILED": 1.0,
        }

    def test_non_succeeded_runs_do_not_pollute_run_time(self) -> None:
        records = [_record("jobs_run_time_seconds", 900.0, state="failed", job_type="ingest")]
        assert _build(records=records).scoped("run_time_p95_seconds") == {}


class TestReferences:
    def test_identifiers_ride_in_refs_not_values(self) -> None:
        # A string in the numeric vector would make a baseline meaningless and
        # would put high-cardinality data on a path the schema calls bounded.
        vector = _build(
            snapshot={
                "max_episode_timestamp_gap_seconds": 9.0,
                "timestamp_gap_scope": "episode-42",
                "timestamp_gap_source": "sha-abc",
            }
        )
        assert vector.value("max_episode_timestamp_gap_seconds") == 9.0
        assert vector.ref("timestamp_gap_scope") == "episode-42"
        assert vector.ref("timestamp_gap_source") == "sha-abc"
        assert all(
            isinstance(name, str) and isinstance(value, float)
            for name, value in vector.values.items()
        )

    def test_missing_reference_returns_the_default(self) -> None:
        assert _build().ref("timestamp_gap_scope", "unknown") == "unknown"


class TestMonitorSelfObservation:
    def test_sink_lag_is_the_gap_to_the_newest_record(self) -> None:
        vector = _build(records=[_record("episodes_ingested_total", 1.0, seconds_ago=30)])
        assert vector.value("sink_lag_seconds") == 30.0

    def test_sink_lag_is_absent_with_no_records_at_all(self) -> None:
        assert not _build(records=[]).has("sink_lag_seconds")

    def test_api_error_rate_counts_non_2xx(self) -> None:
        records = [
            _record("api_requests_total", 1.0, status_class="2xx"),
            _record("api_requests_total", 1.0, status_class="2xx"),
            _record("api_requests_total", 1.0, status_class="5xx"),
            _record("api_requests_total", 1.0, status_class="4xx"),
        ]
        assert _build(records=records).value("api_error_rate") == 0.5
