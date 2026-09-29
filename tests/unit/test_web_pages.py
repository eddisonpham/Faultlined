"""Unit tests for the server-rendered UI helpers (ADR 0014)."""

from __future__ import annotations

import pytest

from data_engine.web import DEFAULT_THEME, THEMES, layout_css, vendor_css
from data_engine.web.pages import (
    _attempts,
    _bytes,
    _cancel_action,
    _deadline,
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


@pytest.mark.unit
def test_attempts_render_a_budget_meter_and_hide_unused_ones() -> None:
    assert "text-comment" in _attempts({}), "a job that has not run should not show a meter"
    used = _attempts({"attempts": 2, "max_attempts": 4})
    assert "2/4" in used
    assert used.count("#") == 5, "half the budget should fill half the meter"
    assert "#" * 11 not in _attempts({"attempts": 9, "max_attempts": 3}), "must not overflow"


@pytest.mark.unit
def test_attempts_survive_a_missing_or_zero_budget() -> None:
    """A malformed row must not divide by zero in a renderer."""
    assert "1/1" in _attempts({"attempts": 1, "max_attempts": 0})
    assert "1/1" in _attempts({"attempts": 1, "max_attempts": None})


@pytest.mark.unit
def test_deadline_reads_as_remaining_budget_and_flags_overdue() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    later = _deadline({"deadline_at": now + timedelta(minutes=90)}, now)
    assert "text-success" in later
    assert "90m left" in later
    assert "data-deadline" in later

    overdue = _deadline({"deadline_at": now - timedelta(minutes=5)}, now)
    assert "text-error" in overdue
    assert "5m over" in overdue

    assert "text-comment" in _deadline({}, now)
    # An unparsable timestamp is shown verbatim rather than dropped: a raw column
    # value is more useful to debug with than a dash.
    assert "not-a-timestamp" in _deadline({"deadline_at": "not-a-timestamp"}, now)


@pytest.mark.unit
def test_deadline_treats_a_naive_timestamp_as_utc() -> None:
    """Postgres returns naive datetimes for some drivers; they are UTC by contract."""
    from datetime import UTC, datetime

    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert "30m left" in _deadline({"deadline_at": datetime(2026, 9, 29, 12, 30)}, now)


@pytest.mark.unit
def test_cancel_action_is_a_form_post_for_live_jobs_only() -> None:
    for state in ("queued", "running", "retrying"):
        action = _cancel_action({"id": "job-1", "state": state}, DEFAULT_THEME)
        assert 'method="post"' in action
        assert "/ui/jobs/job-1/cancel" in action
        assert "<script" not in action, "the control must work without JavaScript"

    for state in ("succeeded", "failed", "canceled", "timed_out"):
        assert "<form" not in _cancel_action({"id": "job-1", "state": state}, DEFAULT_THEME)

    # A job already flagged for cancellation has nothing left to ask for.
    assert "cancel unavailable" in _cancel_action({"id": "j", "state": "cancel_requested"}, "vt220")


@pytest.mark.unit
def test_cancel_action_keeps_the_selected_theme_across_the_redirect() -> None:
    action = _cancel_action({"id": "job-1", "state": "queued"}, "amber")
    assert "cancel?theme=amber" in action


@pytest.mark.unit
def test_job_detail_shows_the_cancel_control_for_a_live_job() -> None:
    html = job_detail_page(
        {
            "id": "job-1",
            "type": "ingest",
            "state": "running",
            "payload": {},
            "result": None,
            "error": None,
            "correlation_id": "corr-1",
            "attempts": 1,
            "max_attempts": 3,
            "deadline_at": "2026-09-29T12:30:00Z",
            "created_at": "2026-09-29T12:00:00Z",
        },
        DEFAULT_THEME,
    )
    assert "de-cancel" in html
    assert "1/3" in html
    assert "data-deadline" in html
