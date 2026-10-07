"""Unit tests for deterministic incident rendering (ADR 0020 §10.2)."""

from __future__ import annotations

from typing import Any

import pytest

from data_engine.monitoring.signals import Label
from data_engine.monitoring.summary import (
    render_digest,
    render_notify,
    render_summary,
)

GIB = 1024**3


class TestActionability:
    def test_every_label_renders_something_with_its_numbers(self) -> None:
        samples: dict[str, tuple[dict[str, Any], str]] = {
            Label.CONTRACT_BREACH.value: (
                {
                    "expected_episodes": 200,
                    "valid_episodes": 118,
                    "shortfall": 82,
                    "job_state": "succeeded",
                },
                "118",
            ),
            Label.PARTIAL_SUCCESS.value: (
                {
                    "expected_valid_fraction": 0.9,
                    "observed_valid_fraction": 0.4,
                    "produced_episodes": 100,
                },
                "40%",
            ),
            Label.WORKER_LOST.value: (
                {
                    "heartbeat_age_seconds": 900.0,
                    "threshold_seconds": 180.0,
                    "queue_depth_queued": 2,
                    "queue_depth_running": 1,
                },
                "900",
            ),
            Label.RUN_STALLED.value: (
                {
                    "job_id": "job-1",
                    "job_type": "ingest",
                    "age_seconds": 2000.0,
                    "deadline_passed": True,
                },
                "job-1",
            ),
            Label.DISK_PRESSURE.value: (
                {"disk_free_bytes": 0.5 * GIB, "threshold_bytes": 2 * GIB},
                "0.5",
            ),
            Label.DATABASE_UNREACHABLE.value: ({}, "unreachable"),
            Label.CLOCK_DRIFT.value: (
                {"max_gap_seconds": 9.0, "source": "sha-abc"},
                "9.0",
            ),
            Label.QUARANTINE_RATE_HIGH.value: (
                {
                    "quarantine_rate": 0.5,
                    "absolute_max": 0.35,
                    "basis": "absolute",
                    "baseline_median": None,
                },
                "50%",
            ),
            Label.QUALITY_SHIFT.value: (
                {
                    "feature": "jerk_mean",
                    "value": 0.9,
                    "robust_z": 12.0,
                    "baseline_median": 0.02,
                    "observations": 30,
                },
                "jerk_mean",
            ),
            Label.FRAME_COUNT_COLLAPSE.value: (
                {"value": 30.0, "ratio_to_center": 0.15, "baseline_center": 200.0},
                "30",
            ),
            Label.REPEATED_READ_FAILURE.value: (
                {"failure_count": 5.0, "reason_code": "READ_FAILED"},
                "READ_FAILED",
            ),
            Label.RESOURCE_DEGRADED.value: (
                {"cpu_percent": 98.0, "memory_used_ratio": 0.95},
                "cpu",
            ),
            Label.METRIC_SHIFT.value: (
                {"shifted": [{"feature": "api_error_rate", "value": 0.5, "robust_z": 9.0}]},
                "api_error_rate",
            ),
            Label.TIME_MISSED.value: (
                {"job_id": "job-2", "overdue_seconds": 300.0, "job_state": "running"},
                "job-2",
            ),
        }
        assert set(samples) == {label.value for label in Label}
        for label, (evidence, expected) in samples.items():
            rendered = render_summary(label, evidence, "fallback detail")
            assert expected in rendered, f"{label} did not cite {expected!r}: {rendered}"
            assert rendered.strip()

    def test_an_unknown_label_falls_back_to_its_detail(self) -> None:
        assert render_summary("SOMETHING_NEW", {"a": 1}, "the detail") == "the detail"

    def test_rendering_is_deterministic(self) -> None:
        evidence = {"expected_episodes": 200, "valid_episodes": 118, "shortfall": 82}
        first = render_summary(Label.CONTRACT_BREACH.value, evidence, "d")
        second = render_summary(Label.CONTRACT_BREACH.value, evidence, "d")
        assert first == second

    def test_a_never_seen_heartbeat_says_so(self) -> None:
        rendered = render_summary(Label.WORKER_LOST.value, {"heartbeat_age_seconds": None}, "d")
        assert "No worker heartbeat seen" in rendered
        assert "900" not in rendered


class TestHonesty:
    def test_quarantine_omits_a_baseline_it_does_not_have(self) -> None:
        cold = render_summary(
            Label.QUARANTINE_RATE_HIGH.value,
            {"quarantine_rate": 0.5, "absolute_max": 0.35, "basis": "absolute"},
            "d",
        )
        assert "usual" not in cold
        assert "hard bound" in cold

    def test_quarantine_cites_the_baseline_when_there_is_one(self) -> None:
        warm = render_summary(
            Label.QUARANTINE_RATE_HIGH.value,
            {
                "quarantine_rate": 0.3,
                "basis": "baseline",
                "baseline_median": 0.02,
                "robust_z": 8.0,
            },
            "d",
        )
        assert "2%" in warm
        assert "robust sigmas" in warm

    def test_summaries_avoid_characters_a_console_cannot_encode(self) -> None:
        for label in Label:
            rendered = render_summary(
                label.value,
                {
                    "shifted": [{"feature": "x", "value": 1.0, "robust_z": 2.0}],
                    "feature": "x",
                    "value": 1.0,
                    "robust_z": 2.0,
                    "baseline_median": 0.0,
                    "observations": 1,
                },
                "d",
            )
            rendered.encode("cp1252")
            assert "\u03c3" not in rendered


def _incident(**kwargs: Any) -> dict[str, Any]:
    base = {
        "id": "inc-1",
        "label": "CONTRACT_BREACH",
        "severity": "critical",
        "notify_class": "notify",
        "summary": "Run produced 0 valid episodes; 5 were expected",
        "occurrence_count": 1,
        "first_seen": "2026-09-29T12:00:00+00:00",
    }
    base.update(kwargs)
    return base


class TestDigest:
    def test_an_empty_queue_reads_as_no_incidents(self) -> None:
        assert render_digest([]) == "No incidents."

    def test_notify_class_incidents_come_first(self) -> None:
        digest = render_digest(
            [
                _incident(id="a", label="METRIC_SHIFT", severity="medium", notify_class="queue"),
                _incident(id="b", label="WORKER_LOST", severity="critical"),
            ]
        )
        assert digest.index("WORKER_LOST") < digest.index("METRIC_SHIFT")
        assert "need attention" in digest
        assert "queued" in digest

    def test_a_queue_with_no_interruptions_reads_as_all_queued(self) -> None:
        digest = render_digest(
            [_incident(notify_class="queue", label="METRIC_SHIFT", severity="medium")]
        )
        assert "1 queued" in digest
        assert "need attention" not in digest

    def test_occurrences_are_shown_for_queued_items(self) -> None:
        digest = render_digest([_incident(notify_class="queue", occurrence_count=7)])
        assert "x7" in digest

    def test_ordering_is_stable_for_equal_severities(self) -> None:
        rows = [
            _incident(id="a", first_seen="2026-09-29T12:00:00+00:00", notify_class="queue"),
            _incident(id="b", first_seen="2026-09-29T11:00:00+00:00", notify_class="queue"),
        ]
        assert render_digest(rows) == render_digest(list(reversed(rows)))


class TestNotifyBody:
    def test_nothing_to_send_renders_empty(self) -> None:
        assert render_notify([]) == ""

    def test_a_single_incident_is_singular(self) -> None:
        assert render_notify([_incident()]) is not None
        assert "work needs attention" in render_notify([_incident()])

    def test_the_body_leads_with_the_incident_and_points_at_the_queue(self) -> None:
        body = render_notify([_incident()])
        assert "CONTRACT_BREACH" in body
        assert "/ui/incidents" in body

    def test_the_body_never_invents_a_cause(self) -> None:
        body = render_notify([_incident(summary="disk is full")])
        assert "disk is full" in body
        with pytest.raises(KeyError):
            render_notify([{"label": "X"}])
