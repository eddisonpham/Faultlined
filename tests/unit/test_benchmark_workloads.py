"""Unit tests for the run-intelligence benchmark workloads (backlog B-003, B-012..B-015)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from benchmarks.harness import (
    WORKLOADS,
    _aggregation_benchmark,
    _client_benchmark,
    _lerobot_benchmark,
    _quality_benchmark,
    _validation_benchmark,
)


@pytest.mark.unit
def test_quality_workload_measures_analysis_latency() -> None:
    result = _quality_benchmark(frames=50, trials=2, warmups=1)
    assert result.status == "ok"
    assert len(result.trials) == 2
    assert result.benchmark.name == "quality-analysis-50f"
    assert result.summary.p50_seconds > 0


@pytest.mark.unit
def test_validation_workload_evaluates_the_full_rule_registry() -> None:
    result = _validation_benchmark(trials=2, warmups=1)
    assert result.status == "ok"
    assert result.benchmark.name == "validation-eval"
    assert result.config["rules"] == 7


@pytest.mark.unit
def test_aggregation_workload_summarizes_a_sized_record_set() -> None:
    result = _aggregation_benchmark(record_count=500, trials=2, warmups=1)
    assert result.status == "ok"
    assert result.config["records"] == 500


@pytest.mark.unit
def test_client_workload_measures_in_process_request_latency(tmp_path: Path) -> None:
    result = _client_benchmark(
        name="probe",
        path="/api/v1/health",
        dataset="synthetic-empty-v1",
        trials=2,
        warmups=1,
    )
    assert result.status == "ok"
    assert result.trials[0].success is True


@pytest.mark.unit
def test_lerobot_workload_skips_cleanly_without_the_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _lerobot_benchmark() is None


@pytest.mark.unit
def test_workload_registry_covers_the_backlog() -> None:
    assert set(WORKLOADS) >= {
        "synthetic-episode-ingest",
        "quality-analysis-303f",
        "quality-analysis-3000f",
        "validation-eval",
        "metrics-aggregation",
        "api-metrics-endpoint",
        "api-episodes-catalog",
        "api-episode-validation",
        "api-job-report",
        "api-episodes-export",
        "api-failures-summary",
        "api-failing-episodes",
        "api-slice-manifest",
        "api-incidents-catalog",
        "monitor-evaluate",
        "ui-insights-page",
        "lerobot-ingest-v3",
        "mcap-ingest",
    }


@pytest.mark.unit
def test_mcap_workload_skips_cleanly_without_the_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from benchmarks.harness import _mcap_benchmark

    monkeypatch.chdir(tmp_path)
    assert _mcap_benchmark() is None


@pytest.mark.unit
def test_the_mcap_workload_reports_throughput_against_a_real_file(
    mcap_log: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B-002 exists to answer "bytes per second", so p50 alone is not a result."""
    from benchmarks.harness import _mcap_benchmark

    monkeypatch.chdir(tmp_path)
    mcap_log.write_log(tmp_path / "var" / "real-data" / "so101_pick_place.mcap", seconds=4.0)
    result = _mcap_benchmark(trials=2, warmups=0)
    assert result is not None
    assert result.status == "ok"
    assert result.config["bytes"] > 0
    assert result.config["throughput_mib_per_second_p50"] > 0.0
    assert len(result.config["source_sha256"]) == 64


@pytest.mark.unit
def test_the_monitor_workload_is_deterministic() -> None:
    from benchmarks.harness import _monitor_evaluate_benchmark

    result = _monitor_evaluate_benchmark(trials=5, warmups=1)
    assert result.status == "ok"
    assert result.summary is not None
    assert result.summary.failure_count == 0
