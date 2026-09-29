"""Deterministic feature vector for the monitoring notifier (ADR 0020).

Pure by construction: no I/O, no clock, no randomness. Every value is a function
of its inputs, so a recorded window replays to the same vector and detector tests
need neither a database nor a running platform.

Three invariants exist because breaking each one produces a monitor that fails in
production rather than in a test:

* **No unbounded labels.** Route templates and job types only. A feature keyed by
  a job id would create a baseline per job, which never warms up and never means
  anything. (Same rule as ADR 0017's metric labels.)
* **Missing is not zero.** An absent sensor yields an *absent feature*, not a
  ``0.0``. The signals an operator most needs are the ones that go missing, and
  the metadata that says so is itself observable: ``sensor_availability`` counts
  how many expected features are present.
* **Versioned schema.** :data:`FEATURE_SCHEMA_VERSION` is recorded on every
  incident, so a schema change retires stored baselines explicitly instead of
  silently mis-scoring history against limits computed for a different meaning.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from data_engine.observability.aggregate import heartbeat_age_seconds, percentile

FEATURE_SCHEMA_VERSION = 1

# Fixed order and fixed width. A detector indexes by name, but the tuple is the
# contract: it is what makes "the vector" a well-defined object to assert on.
FEATURE_NAMES: tuple[str, ...] = (
    # queue and scheduling
    "queue_depth_queued",
    "queue_depth_running",
    "queue_depth_failed",
    "queue_oldest_age_seconds",
    # run outcomes
    "job_failure_rate",
    "job_retry_count",
    "job_cancel_count",
    "job_timeout_count",
    # progress
    "episodes_ingested",
    "artifacts_bytes_written",
    "episodes_per_second",
    # curation
    "quarantine_rate",
    "verdict_jerky_rate",
    "movement_mean",
    "jerk_mean",
    "stall_mean",
    "frame_count_median",
    # health
    "heartbeat_age_seconds",
    "catalog_query_p95_seconds",
    "api_request_p95_seconds",
    "api_error_rate",
    # resources
    "cpu_percent",
    "memory_used_ratio",
    "disk_free_bytes",
    # monitor's own view
    "sink_lag_seconds",
    "sensor_availability",
)

# Features that may legitimately be absent, with the count that defines
# ``sensor_availability``. A sensor that is always present contributes nothing
# to the count, so the denominator is the set of sensors that *can* go missing.
_OPTIONAL_SENSORS: frozenset[str] = frozenset(
    {
        "queue_oldest_age_seconds",
        "quarantine_rate",
        "verdict_jerky_rate",
        "movement_mean",
        "jerk_mean",
        "stall_mean",
        "frame_count_median",
        "heartbeat_age_seconds",
        "catalog_query_p95_seconds",
        "api_request_p95_seconds",
        "cpu_percent",
        "memory_used_ratio",
        "disk_free_bytes",
    }
)

DEFAULT_WINDOW_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """Host resources at tick time; built by the caller so this module stays pure."""

    cpu_percent: float | None = None
    memory_used_bytes: float | None = None
    memory_available_bytes: float | None = None
    disk_free_bytes: float | None = None


@dataclass(frozen=True, slots=True)
class FeatureInputs:
    """Everything one tick is allowed to look at."""

    now: datetime
    records: Sequence[Mapping[str, Any]]
    snapshot: Mapping[str, Any]
    resources: ResourceSample | None = None
    window_seconds: float = DEFAULT_WINDOW_SECONDS
    catalog_reachable: bool = True
    metrics_path_mtime: datetime | None = None


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """One evaluation window.

    ``scopes`` holds per-scope numeric variants (e.g. run time by job type).
    ``refs`` holds routing identifiers (e.g. which episode had the timestamp gap).
    Identifiers live here rather than in ``values`` because a feature must be a
    number: a string in the vector would make a baseline meaningless and would put
    a high-cardinality value on a path the schema promises is bounded.
    """

    values: Mapping[str, float]
    scopes: Mapping[str, Mapping[str, float]]
    refs: Mapping[str, str] = MappingProxyType({})
    schema_version: int = FEATURE_SCHEMA_VERSION
    catalog_reachable: bool = True
    window_seconds: float = DEFAULT_WINDOW_SECONDS

    def value(self, name: str, default: float = 0.0) -> float:
        """The value, or ``default`` when the sensor was absent.

        Callers that must distinguish "absent" from "zero" check :meth:`has`
        first; that distinction is the whole point of the missing-is-not-zero rule.
        """
        return float(self.values.get(name, default))

    def has(self, name: str) -> bool:
        return name in self.values

    def scoped(self, name: str) -> Mapping[str, float]:
        return self.scopes.get(name, {})

    def scoped_pairs(self, name: str) -> list[tuple[str, float]]:
        """``(scope, value)`` pairs, sorted so iteration order never varies."""
        return sorted(self.scopes.get(name, {}).items())

    def ref(self, name: str, default: str = "") -> str:
        return self.refs.get(name, default)


def build_features(inputs: FeatureInputs) -> FeatureVector:
    """Reduce one window of telemetry and catalog state to the fixed vector."""
    records = list(inputs.records)
    snapshot = dict(inputs.snapshot)
    values: dict[str, float] = {}
    scopes: dict[str, dict[str, float]] = {}
    refs: dict[str, str] = {}

    _queue_features(snapshot, values)
    _run_features(records, values, scopes)
    _progress_features(records, values, inputs.window_seconds)
    _curation_features(snapshot, values, refs)
    _health_features(records, values, inputs.now)
    _resource_features(inputs.resources, values)
    _monitor_features(records, inputs, values)

    values["sensor_availability"] = _sensor_availability(values)
    return FeatureVector(
        values=values,
        scopes=scopes,
        refs=MappingProxyType(refs),
        catalog_reachable=inputs.catalog_reachable,
        window_seconds=inputs.window_seconds,
    )


def _queue_features(snapshot: Mapping[str, Any], values: dict[str, float]) -> None:
    depth = {str(k): int(v) for k, v in dict(snapshot.get("queue_depth") or {}).items()}
    values["queue_depth_queued"] = float(depth.get("queued", 0))
    values["queue_depth_running"] = float(depth.get("running", 0))
    values["queue_depth_failed"] = float(depth.get("failed", 0))
    oldest = snapshot.get("oldest_queued_age_seconds")
    if oldest is not None:
        values["queue_oldest_age_seconds"] = float(oldest)


def _run_features(
    records: Sequence[Mapping[str, Any]],
    values: dict[str, float],
    scopes: dict[str, dict[str, float]],
) -> None:
    failures = _values(records, "jobs_failures_total")
    succeeded = _values(records, "jobs_run_time_seconds", label=("state", "succeeded"))
    settled = len(failures) + len(succeeded)
    values["job_failure_rate"] = (len(failures) / settled) if settled else 0.0
    values["job_retry_count"] = float(len(_values(records, "jobs_retries_total")))
    values["job_cancel_count"] = float(len(_values(records, "jobs_cancellations_total")))
    values["job_timeout_count"] = float(len(_values(records, "jobs_timeouts_total")))

    # Run time is scoped by job type: a validation tick and an ingest of a
    # 200-episode dataset are not the same quantity, and a single global limit
    # would flag the faster one forever. Per-scope baselines are what make
    # "this job type is normally slower" pass unflagged (ADR 0020).
    by_type: dict[str, list[float]] = {}
    for record in records:
        if record.get("name") != "jobs_run_time_seconds":
            continue
        labels = record.get("labels") or {}
        if str(labels.get("state")) != "succeeded":
            continue
        job_type = str(labels.get("job_type") or "unknown")
        by_type.setdefault(job_type, []).append(float(record["value"]))
    if by_type:
        scopes["run_time_p95_seconds"] = {
            job_type: percentile(sorted(samples), 0.95) for job_type, samples in by_type.items()
        }

    failures_by_reason: dict[str, int] = {}
    for record in records:
        if record.get("name") != "jobs_failures_total":
            continue
        reason = str((record.get("labels") or {}).get("reason_code") or "unknown")
        failures_by_reason[reason] = failures_by_reason.get(reason, 0) + 1
    if failures_by_reason:
        scopes["failure_count"] = {k: float(v) for k, v in sorted(failures_by_reason.items())}


def _progress_features(
    records: Sequence[Mapping[str, Any]], values: dict[str, float], window_seconds: float
) -> None:
    ingested = _values(records, "episodes_ingested_total")
    values["episodes_ingested"] = float(len(ingested))
    values["artifacts_bytes_written"] = float(
        sum(_values(records, "artifacts_written_bytes_total"))
    )
    values["episodes_per_second"] = len(ingested) / window_seconds if window_seconds > 0 else 0.0


def _curation_features(
    snapshot: Mapping[str, Any], values: dict[str, float], refs: dict[str, str]
) -> None:
    rate = snapshot.get("quarantine_rate")
    if rate is not None:
        values["quarantine_rate"] = float(rate)
    verdicts = dict(snapshot.get("verdict_rates") or {})
    total = sum(verdicts.values())
    if total:
        values["verdict_jerky_rate"] = float(verdicts.get("jerky", 0.0) / total)
    for key, field in (
        ("movement_mean", "movement_mean"),
        ("jerk_mean", "jerk_mean"),
        ("stall_mean", "stall_mean"),
    ):
        value = snapshot.get(field)
        if value is not None:
            values[key] = float(value)
    frame_median = snapshot.get("frame_count_median")
    if frame_median is not None:
        values["frame_count_median"] = float(frame_median)
    gap = snapshot.get("max_episode_timestamp_gap_seconds")
    if gap is not None:
        values["max_episode_timestamp_gap_seconds"] = float(gap)
        # Routing identifiers for the gap, kept out of the numeric vector.
        for key in ("timestamp_gap_scope", "timestamp_gap_source"):
            raw = snapshot.get(key)
            if raw:
                refs[key] = str(raw)


def _health_features(
    records: Sequence[Mapping[str, Any]], values: dict[str, float], now: datetime
) -> None:
    # heartbeat_age_seconds is None when no heartbeat was ever seen. That is a
    # *feature* (the worker is not reporting at all), not a missing feature, so
    # the caller decides: see the WORKER_LOST rule in detectors.py.
    age = heartbeat_age_seconds([dict(record) for record in records], now=now)
    if age is not None:
        values["heartbeat_age_seconds"] = age

    catalog = sorted(_values(records, "catalog_query_duration_seconds"))
    if catalog:
        values["catalog_query_p95_seconds"] = percentile(catalog, 0.95)
    api = sorted(_values(records, "api_request_duration_seconds"))
    if api:
        values["api_request_p95_seconds"] = percentile(api, 0.95)
    requests = records_with(records, "api_requests_total")
    if requests:
        errored = sum(
            1
            for record in requests
            if str((record.get("labels") or {}).get("status_class", "")).startswith(("4", "5"))
        )
        values["api_error_rate"] = errored / len(requests)


def _resource_features(resources: ResourceSample | None, values: dict[str, float]) -> None:
    if resources is None:
        return
    if resources.cpu_percent is not None:
        values["cpu_percent"] = float(resources.cpu_percent)
    used, available = resources.memory_used_bytes, resources.memory_available_bytes
    if used is not None and available is not None and (used + available) > 0:
        values["memory_used_ratio"] = float(used / (used + available))
    if resources.disk_free_bytes is not None:
        values["disk_free_bytes"] = float(resources.disk_free_bytes)


def _monitor_features(
    records: Sequence[Mapping[str, Any]], inputs: FeatureInputs, values: dict[str, float]
) -> None:
    """The monitor's own view of its inputs.

    ``sink_lag_seconds`` is the gap between the newest metric record and now. It
    deliberately does not become an incident: a monitor that cannot see should
    report itself blind on ``/api/v1/monitoring/health`` rather than paging the
    operator about a platform it is not actually observing.
    """
    newest = _newest_timestamp(records)
    if newest is not None:
        values["sink_lag_seconds"] = max(0.0, (inputs.now - newest).total_seconds())
    elif inputs.metrics_path_mtime is not None:
        values["sink_lag_seconds"] = max(
            0.0, (inputs.now - inputs.metrics_path_mtime).total_seconds()
        )
    if not inputs.catalog_reachable:
        # Every catalog-derived feature is untrustworthy this tick; recording a
        # zero for them would look like a healthy, empty system.
        for name in ("quarantine_rate", "frame_count_median", "verdict_jerky_rate"):
            values.pop(name, None)


def _sensor_availability(values: Mapping[str, float]) -> float:
    present = sum(1 for name in _OPTIONAL_SENSORS if name in values)
    return present / len(_OPTIONAL_SENSORS)


def _values(
    records: Sequence[Mapping[str, Any]],
    name: str,
    *,
    label: tuple[str, str] | None = None,
) -> list[float]:
    out: list[float] = []
    for record in records:
        if record.get("name") != name:
            continue
        if label is not None:
            key, expected = label
            if str((record.get("labels") or {}).get(key, "")) != expected:
                continue
        out.append(float(record["value"]))
    return out


def records_with(records: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    """Records for one metric name; shared with the health endpoint's error rate."""
    return [record for record in records if record.get("name") == name]


def _newest_timestamp(records: Sequence[Mapping[str, Any]]) -> datetime | None:
    newest: datetime | None = None
    for record in records:
        raw = record.get("timestamp")
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if newest is None or parsed > newest:
            newest = parsed
    return newest
