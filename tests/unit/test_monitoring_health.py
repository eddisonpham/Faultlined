"""Monitor health must answer for whichever process actually ticked (ADR 0031).

The tick runs in the worker loop; `/api/v1/monitoring/health` is served by the API
process. An in-memory answer therefore reported `last_tick_at: null` and rendered
`blind: no` about a monitor that was running - the exact false negative the health
surface exists to prevent. These tests pin the read-back from the shared metric
sink, with no database and no second process.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from data_engine.monitoring.service import MonitorService


def _record(name: str, value: float, timestamp: datetime) -> str:
    return json.dumps(
        {
            "name": name,
            "value": value,
            "unit": "x",
            "labels": {},
            "timestamp": timestamp.isoformat(),
            "source": "runtime",
            "correlation_id": None,
        }
    )


def _sink(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "runtime.jsonl"
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


@pytest.mark.unit
def test_health_reads_the_tick_another_process_recorded(tmp_path: Path) -> None:
    ticked_at = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    path = _sink(
        tmp_path,
        _record("monitor_tick_seconds", 0.25, ticked_at),
        _record("monitor_blind", 1.0, ticked_at),
    )

    health = MonitorService(object(), metrics_path=path).health()

    assert health["last_tick_at"] == ticked_at.isoformat()
    assert health["blind"] is True


@pytest.mark.unit
def test_health_prefers_the_most_recent_tick_across_processes(tmp_path: Path) -> None:
    early = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    late = early + timedelta(minutes=1)
    path = _sink(
        tmp_path,
        _record("monitor_tick_seconds", 0.25, early),
        _record("monitor_blind", 1.0, early),
        _record("monitor_tick_seconds", 0.20, late),
        _record("monitor_blind", 0.0, late),
    )

    health = MonitorService(object(), metrics_path=path).health()

    assert health["last_tick_at"] == late.isoformat()
    assert health["blind"] is False


@pytest.mark.unit
def test_health_says_never_rather_than_healthy_when_nothing_has_ticked(
    tmp_path: Path,
) -> None:
    """`blind: null` is not `blind: false`: no tick has run, so nothing is known."""
    health = MonitorService(object(), metrics_path=tmp_path / "missing.jsonl").health()

    assert health["last_tick_at"] is None
    assert health["blind"] is None


@pytest.mark.unit
def test_health_reads_the_sink_only_to_the_bounded_tail(tmp_path: Path) -> None:
    """A health check must not walk the whole history to find the last tick."""
    from data_engine.monitoring import service as monitor_service

    tail = monitor_service.HEALTH_TAIL_RECORDS
    ticked_at = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
    filler = [_record("jobs_run_time_seconds", float(i), ticked_at) for i in range(tail + 50)]
    path = _sink(tmp_path, _record("monitor_tick_seconds", 0.25, ticked_at), *filler)

    health = MonitorService(object(), metrics_path=path).health()

    # The only tick record is older than the tail window, so it is honestly
    # reported as not observed rather than found by scanning the whole file.
    assert health["last_tick_at"] is None
