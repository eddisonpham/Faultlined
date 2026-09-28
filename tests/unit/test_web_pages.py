"""Unit tests for the server-rendered UI helpers (ADR 0014)."""

from __future__ import annotations

import pytest

from data_engine.web import pages
from data_engine.web.pages import _bytes, _state, _when, stylesheet


@pytest.mark.unit
def test_stylesheet_is_served_from_a_real_file() -> None:
    css = stylesheet()
    assert "--accent" in css
    assert "<" not in css.split("{")[0], "stylesheet must not be HTML-escaped"


@pytest.mark.unit
def test_bytes_formatting_is_human_readable() -> None:
    assert _bytes(0) == "0 B"
    assert _bytes(2048) == "2.0 KiB"
    assert _bytes(5 * 1024 * 1024) == "5.0 MiB"


@pytest.mark.unit
def test_bytes_handles_missing_telemetry() -> None:
    assert "n/a" in _bytes(None)


@pytest.mark.unit
def test_when_renders_a_dash_for_missing_timestamps() -> None:
    assert ">" in _when(None)
    assert _when("2026-09-28T12:00:00Z").startswith("2026-09-28 12:00:00")


@pytest.mark.unit
def test_state_gets_a_colour_class_but_unknown_states_stay_plain() -> None:
    assert 'class="s"' in _state("succeeded")
    assert 'class="f"' in _state("failed")
    assert "<" not in _state("brand-new-state")


@pytest.mark.unit
def test_pages_escape_untrusted_values() -> None:
    """Episode/artifact data is user-controlled and must never inject markup."""
    html = pages.artifacts_page(
        {"items": [{"hash": "<script>x</script>", "size_bytes": 1, "episode_ids": []}]}
    )
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.unit
def test_job_detail_escapes_payload_and_renders_error() -> None:
    html = pages.job_detail_page(
        {
            "id": "job-1",
            "type": "ingest",
            "state": "failed",
            "payload": {"bad": "<img src=x onerror=alert(1)>"},
            "result": None,
            "error": {"code": "boom"},
            "correlation_id": "corr-1",
            "created_at": "2026-09-28T00:00:00Z",
            "started_at": "2026-09-28T00:00:01Z",
            "finished_at": None,
        }
    )
    assert "<img src=x" not in html
    assert "boom" in html
    assert "corr-1" in html


@pytest.mark.unit
def test_empty_states_render_a_message_not_a_broken_table() -> None:
    assert "No jobs yet." in pages.jobs_page({"items": []}, None)
    assert "No artifacts yet." in pages.artifacts_page({"items": []})


@pytest.mark.unit
def test_status_body_counts_active_jobs_across_states() -> None:
    body = pages.status_fragment(
        {
            "status": "ok",
            "queue_depth": {"queued": 2, "running": 1, "retrying": 0, "succeeded": 9},
            "artifact_count": 4,
            "episode_count": 4,
            "resources": {"cpu_percent": 12.5, "gpu_present": False},
        }
    )
    assert "Active jobs" in body
    assert "12.5%" in body
    assert "none" in body, "absent GPU should read as none, not blank"


@pytest.mark.unit
def test_poll_script_targets_the_fragment_endpoint() -> None:
    html = pages.status_page({"status": "ok", "queue_depth": {}, "resources": {}})
    assert '"/ui"' in html
    assert "X-Fragment" in html
