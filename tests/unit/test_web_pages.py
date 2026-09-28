"""Unit tests for the server-rendered UI helpers (ADR 0014)."""

from __future__ import annotations

import pytest

from data_engine.web import DEFAULT_THEME, THEMES, layout_css, vendor_css
from data_engine.web.pages import (
    _bytes,
    _meter,
    _state_badge,
    _strip,
    _when,
    artifacts_page,
    job_detail_page,
    jobs_page,
    status_page,
)


@pytest.mark.unit
def test_vendored_stylesheets_are_present_and_self_contained() -> None:
    """The vendored CSS must ship in-tree and must not fetch anything at runtime."""
    assert "--fine-use" in vendor_css("core.css")
    for theme in THEMES:
        css = vendor_css(f"theme-{theme}.css")
        assert "--fine-use-bg" in css
        assert "@import" not in css
        assert "http://" not in css and "https://" not in css


@pytest.mark.unit
def test_layout_layer_defines_our_own_class_names() -> None:
    assert ".de-readouts" in layout_css()
    assert ".de-meter" in layout_css()


@pytest.mark.unit
def test_bytes_formatting_is_human_readable() -> None:
    assert _bytes(0) == "0 B"
    assert _bytes(2048) == "2.0 KiB"
    assert _bytes(5 * 1024 * 1024) == "5.0 MiB"
    assert _bytes(None) == "n/a"


@pytest.mark.unit
def test_when_renders_a_placeholder_for_missing_timestamps() -> None:
    assert "text-comment" in _when(None)
    assert _when("2026-09-28T12:00:00Z") == "2026-09-28 12:00:00"


@pytest.mark.unit
def test_state_badges_colour_terminally() -> None:
    assert "text-success" in _state_badge("succeeded")
    assert "text-error" in _state_badge("failed")
    assert "text-comment" in _state_badge("brand-new-state")


@pytest.mark.unit
def test_meter_is_clamped_to_its_width() -> None:
    assert _meter(0.0, 10).count("#") == 0
    assert _meter(1.0, 10).count("#") == 10
    assert _meter(5.0, 10).count("#") == 10, "over-range values must not overflow the meter"
    assert _meter(-1.0, 10).count("#") == 0


@pytest.mark.unit
def test_strip_marks_the_current_step_and_finished_ones() -> None:
    strip = _strip("succeeded")
    assert 'class="step now">succeeded' in strip
    assert 'class="step done">queued' in strip


@pytest.mark.unit
def test_strip_routes_failures_to_a_failing_terminal_state() -> None:
    strip = _strip("failed")
    assert ">failed<" in strip
    assert ">succeeded<" not in strip


@pytest.mark.unit
def test_pages_escape_untrusted_values() -> None:
    """Episode and artifact data is user-controlled and must never inject markup."""
    html = artifacts_page(
        {"items": [{"hash": "<script>x</script>", "size_bytes": 1, "episode_ids": []}]},
        DEFAULT_THEME,
    )
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.unit
def test_job_detail_escapes_payload_and_renders_error() -> None:
    html = job_detail_page(
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
        },
        DEFAULT_THEME,
    )
    assert "<img src=x" not in html
    assert "boom" in html
    assert "corr-1" in html


@pytest.mark.unit
def test_empty_states_read_like_a_terminal() -> None:
    assert "// no jobs recorded" in jobs_page({"items": []}, None, DEFAULT_THEME)
    assert "// no artifacts stored" in artifacts_page({"items": []}, DEFAULT_THEME)


@pytest.mark.unit
def test_status_body_shows_readouts_and_ascii_meters() -> None:
    html = status_page(
        {
            "status": "ok",
            "queue_depth": {"queued": 2, "running": 1, "succeeded": 9},
            "artifact_count": 4,
            "episode_count": 4,
            "resources": {"cpu_percent": 12.5, "gpu_present": False},
        },
        DEFAULT_THEME,
    )
    assert "12.5" in html
    assert "active jobs" in html.lower()
    assert "[" in html and "]" in html, "queue depth should render as an ASCII meter"
    assert "absent" in html, "missing GPU should read as absent, not blank"


@pytest.mark.unit
def test_selected_theme_is_applied_and_pinned() -> None:
    html = status_page({"status": "ok", "queue_depth": {}, "resources": {}}, "amber")
    assert 'data-theme="amber"' in html
    assert "theme-amber.css" in html


@pytest.mark.unit
def test_poll_script_targets_the_fragment_endpoint() -> None:
    html = jobs_page({"items": []}, None, DEFAULT_THEME)
    assert '"/ui/jobs"' in html
    assert "X-Fragment" in html
