"""Unit tests for the server-rendered UI helpers (ADR 0014)."""

from __future__ import annotations

import pytest

from data_engine.web import DEFAULT_THEME, THEMES, layout_css, vendor_css
from data_engine.web.pages import (
    _attempts,
    _bytes,
    _cancel_action,
    _deadline,
    _empty,
    _histogram_svg,
    _insights_body,
    _meter,
    _metrics_body,
    _scatter_svg,
    _sparkline,
    _state_badge,
    _strip,
    _verdict_badge,
    _when,
    artifacts_page,
    episode_detail_page,
    episodes_page,
    font_bytes,
    incidents_fragment,
    incidents_page,
    insights_page,
    job_detail_page,
    jobs_page,
    metrics_page,
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
def test_poll_region_targets_the_fragment_endpoint() -> None:
    """The page declares its poll target as data; app.js sends X-Fragment."""
    html = jobs_page({"items": []}, None, DEFAULT_THEME)
    assert 'data-poll="/ui/jobs"' in html
    assert 'data-poll-ms="3000"' in html
    # The interval is per-section and follows the documented cadence.
    assert 'data-poll="/ui"' in status_page({}, DEFAULT_THEME)
    assert 'data-poll-ms="5000"' in artifacts_page({}, DEFAULT_THEME)


@pytest.mark.unit
def test_polling_replaces_only_the_live_region_not_the_page_chrome() -> None:
    """A poll that swapped <main> would silently delete the filter forms.

    This is the bug the innermost-region rule exists to prevent, so it is
    asserted structurally rather than left to a browser check.
    """
    html = jobs_page({"items": []}, None, DEFAULT_THEME)
    live = html.index('class="de-live"')
    filters = html.index('class="de-filters"')
    assert live < filters, "the poll region must be inside, not around, the filters"
    assert "</main>" in html[live:], "the live region must close before </main>"


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


# ------------------------------------------- run-intelligence pages (ADR 0017/18)


@pytest.mark.unit
def test_font_is_vendored_and_nothing_is_fetched_remotely() -> None:
    assert font_bytes()[:4] == b"wOF2"
    css = layout_css()
    assert "@font-face" in css
    assert 'url("vendor/departure-mono/DepartureMono-Regular.woff2")' in css
    assert "http://" not in css and "https://" not in css


@pytest.mark.unit
def test_sparkline_draws_a_trace_even_without_samples() -> None:
    assert '<polyline points="' in _sparkline([1.0, 2.0, 1.5])
    assert "<svg" in _sparkline([])


@pytest.mark.unit
def test_histogram_bars_and_scatter_dots_scale() -> None:
    bars = _histogram_svg([{"lo": 0, "hi": 1, "count": 1}, {"lo": 1, "hi": 2, "count": 3}])
    assert bars.count("<rect") == 2
    dots = _scatter_svg([{"v": 0.1}, {"v": 0.2}, {"v": 0.3}], "v")
    assert dots.count("<circle") == 3
    assert "<svg" in _histogram_svg([])


@pytest.mark.unit
def test_metrics_body_reports_latency_and_a_stale_worker() -> None:
    model = {
        "record_count": 12,
        "summaries": [
            {
                "name": "api_request_duration_seconds",
                "labels": {
                    "route": "/api/v1/jobs/{job_id}",
                    "method": "GET",
                    "status_class": "2xx",
                },
                "count": 5,
                "p50": 0.002,
                "p95": 0.004,
                "p99": 0.005,
            },
            {
                "name": "pipeline_stage_duration_seconds",
                "labels": {"stage": "ingest", "status": "succeeded"},
                "count": 3,
                "p50": 0.01,
                "p95": 0.02,
                "p99": 0.03,
            },
        ],
        "series": {"api_request_duration_seconds": [{"t": "t", "v": 0.003, "count": 5}]},
        "jobs_queue_depth": {"queued": 2},
        "worker_heartbeat_age_seconds": 45.0,
    }
    body = _metrics_body(model)
    assert "45s stale" in body
    # The route is reported as the operation it performs. The raw template is an
    # internal label and must not reach the operator's screen.
    assert "job listing" in body
    assert "/api/v1" not in body
    assert "Time spent handling requests" in body
    assert "request handling" in body
    assert "4.0" in body  # p95 ms of that operation


@pytest.mark.unit
def test_the_ingest_form_appears_only_on_a_genuinely_empty_system() -> None:
    """It is the only way data gets in, so it leads an empty system...

    ...and disappears the moment there is anything to look at. Left permanently
    on the Status page it becomes a form nobody reads sitting above the numbers
    they actually came for.
    """
    empty = {
        "status": "ok",
        "queue_depth": {},
        "artifact_count": 0,
        "episode_count": 0,
        "resources": {},
    }
    assert "queue ingest job" in status_page(dict(empty), "vt220")

    for populated in (
        {**empty, "queue_depth": {"queued": 1}},
        {**empty, "episode_count": 1},
    ):
        assert "queue ingest job" not in status_page(dict(populated), "vt220")


@pytest.mark.unit
def test_a_rejected_submission_reopens_the_form_on_a_busy_system() -> None:
    """The error renders *inside* the form, so the gate must yield to it.

    Found by driving the real form: gating purely on emptiness meant a bad
    submission on a populated system returned 200 with the message going
    nowhere - no error, no form, nothing to correct.
    """
    busy = {
        "status": "ok",
        "queue_depth": {"succeeded": 4},
        "artifact_count": 4,
        "episode_count": 4,
        "resources": {},
        "ingest_error": "Frames must be a whole number.",
        "ingest_values": {"kind": "episode", "task": "pick_place", "frames": "abc"},
    }
    body = status_page(busy, "vt220")
    assert "queue ingest job" in body, "the form must come back to be fixable"
    assert "Frames must be a whole number." in body
    assert 'value="pick_place"' in body


@pytest.mark.unit
def test_the_ingest_form_survives_a_rejected_submission() -> None:
    """An error must land next to the form, with the typed values kept."""
    model = {
        "status": "ok",
        "queue_depth": {},
        "artifact_count": 0,
        "episode_count": 0,
        "resources": {},
        "ingest_error": "Frames must be a whole number.",
        "ingest_values": {"kind": "episode", "task": "pick_place", "frames": "abc"},
    }
    body = status_page(model, "vt220")
    assert "Frames must be a whole number." in body
    assert 'value="pick_place"' in body
    assert 'value="abc"' in body


@pytest.mark.unit
def test_the_ingest_form_is_a_post_that_needs_no_javascript() -> None:
    body = status_page(
        {
            "status": "ok",
            "queue_depth": {},
            "artifact_count": 0,
            "episode_count": 0,
            "resources": {},
        },
        "vt220",
    )
    assert '<form class="de-ingest" method="post" action="/ui/jobs">' in body
    # Every control is a real named field with a label bound to it.
    for name in ("kind", "task", "robot", "frames", "source"):
        assert f'name="{name}"' in body
        assert f'for="ing-{name}"' in body


@pytest.mark.unit
def test_an_empty_state_link_is_escaped_on_b_sides() -> None:
    """`hint` is escaped; `link` is markup. Exactly one of them may be trusted.

    Both halves have to escape or one of two failures is available: an author's
    <a> silently rendering as literal text, or a value reaching the page raw.
    """
    rendered = _empty(
        "nothing here",
        "a <script>alert(1)</script> hint",
        ("go <b>here</b>", '/ui"><script>alert(2)</script>'),
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    # ...and the legitimate case still produces a real link, not escaped text.
    linked = _empty("no jobs", "start here", ("status page", "/ui"))
    assert '<a href="/ui">status page</a>' in linked


@pytest.mark.unit
def test_metrics_body_marks_a_worker_that_never_reported() -> None:
    body = _metrics_body(
        {
            "summaries": [],
            "series": {},
            "jobs_queue_depth": {},
            "worker_heartbeat_age_seconds": None,
        }
    )
    assert "no heartbeat" in body
    assert "// no request traffic in window" in body


@pytest.mark.unit
def test_verdict_badges_colour_by_severity() -> None:
    assert 'text-success">smooth' in _verdict_badge("smooth")
    assert 'text-warning">moderate' in _verdict_badge("moderate")
    assert 'text-error">jerky' in _verdict_badge("jerky")
    assert 'text-comment">unknown' in _verdict_badge("unknown")


@pytest.mark.unit
def test_episodes_page_renders_quality_columns_and_escapes() -> None:
    html = episodes_page(
        {
            "items": [
                {
                    "id": "e-1",
                    "episode_key": "<script>alert(1)</script>",
                    "format": "lerobot-v3",
                    "state": "valid",
                    "frame_count": 303,
                    "movement_score": 0.04,
                    "jerk_score": 0.01,
                    "stall_ratio": 0.1,
                    "verdict": "jerky",
                    "created_at": "2026-09-29T00:00:00Z",
                }
            ]
        },
        None,
        "jerky",
        DEFAULT_THEME,
    )
    assert "&lt;script&gt;" in html and "<script>alert" not in html
    assert 'text-error">jerky' in html
    assert "0.0400" in html
    assert "curation view" in html


@pytest.mark.unit
def test_episode_detail_renders_the_quality_panel_with_dims() -> None:
    episode = {
        "id": "e-1",
        "episode_key": "episode_index=0",
        "format": "lerobot-v3",
        "state": "valid",
        "metadata": {
            "robot": "so100_follower",
            "task": "pick",
            "frame_count": 303,
            "duration_seconds": 10.06,
            "fps": 30,
            "channel_stats": {"action": {"count": 303, "min": 0.0, "max": 1.0}},
        },
    }
    quality = {
        "verdict": "smooth",
        "movement_score": 0.04,
        "jerk_score": 0.01,
        "stall_ratio": 0.1,
        "length_zscore": 0.2,
        "frame_count": 303,
        "dims": [
            {
                "name": "action[0]",
                "active": True,
                "discrete": False,
                "gripper": False,
                "norm_delta_std": 0.01,
                "mean_abs_delta_norm": 0.005,
            }
        ],
    }
    html = episode_detail_page(episode, quality, DEFAULT_THEME)
    assert "Motion quality" in html
    assert "action[0]" in html
    assert "Channels" in html
    assert "so100_follower" in html

    without = episode_detail_page(episode, None, DEFAULT_THEME)
    assert "no quality signals" in without


@pytest.mark.unit
def test_insights_body_renders_distributions_heat_and_outliers() -> None:
    body = _insights_body(
        {
            "episode_count": 2,
            "verdicts": {"smooth": 1, "jerky": 1},
            "length": {
                "count": 2,
                "mean": 250.0,
                "std": 70.0,
                "min": 200,
                "max": 300,
                "histogram": [
                    {"lo": 200, "hi": 250, "count": 1},
                    {"lo": 250, "hi": 300, "count": 1},
                ],
            },
            "speed_distribution": [
                {"episode_id": "e-1", "movement_score": 0.1, "verdict": "smooth"},
                {"episode_id": "e-2", "movement_score": 0.9, "verdict": "jerky"},
            ],
            "heat_matrix": {
                "dims": ["action[0]", "action[1]"],
                "episodes": [
                    {"episode_id": "e-1", "values": [0.01, None]},
                    {"episode_id": "e-2", "values": [0.2, 0.1]},
                ],
            },
            "outliers": {
                "jerk": [
                    {
                        "episode_id": "e-2",
                        "episode_key": "episode_index=1",
                        "value": 0.9,
                        "verdict": "jerky",
                    }
                ],
                "stall": [],
                "length": [],
            },
        }
    )
    assert "Cross-episode variance" in body
    assert "de-heat" in body
    assert "/ui/episodes/e-2" in body
    assert "top jerk" in body
    assert "Motion, comparable across datasets" in body
    assert "Episode lengths" in body


@pytest.mark.unit
def test_the_motion_chart_is_plotted_on_the_dimensionless_score() -> None:
    """The chart this replaced plotted a quantity with no shared unit.

    `movement_score` is an L2 norm in the source's own units, so the reference
    run put a driving fixture at 1.0e8 beside arm joints at 0.02 and four of the
    five episodes collapsed onto the floor of a linear axis. `jerk_score` is the
    same motion divided by each dimension's range, so the axis is meaningful
    across datasets. Both numbers are still shown, because "how far did this joint
    actually travel" is a real question with a real-unit answer.
    """
    body = _insights_body(
        {
            "episode_count": 2,
            "verdicts": {"smooth": 1, "moderate": 1},
            "length": {"mean": 10.0, "std": 1.0, "min": 5, "max": 20, "histogram": []},
            "speed_distribution": [
                {
                    "episode_id": "e-1",
                    "movement_score": 1.0e8,
                    "jerk_score": 0.018,
                    "integrity": "ok",
                    "verdict": "moderate",
                },
                {
                    "episode_id": "e-2",
                    "movement_score": 0.02,
                    "jerk_score": 0.004,
                    "integrity": "gapped",
                    "verdict": "smooth",
                },
            ],
            "integrity": {"gapped": 1, "ok": 1},
            "heat_matrix": {},
            "outliers": {},
        }
    )
    # The dots are placed from jerk_score, so a 1e8 raw score cannot drag the
    # small episode to the floor: the two dots sit at clearly different heights
    # and the axis is labelled in the normalised unit.
    assert "log10" not in body  # 0.004..0.018 is under two decades; linear is right
    assert "0.018" in body
    assert "Recording reliability" in body
    assert "gapped" in body
    # Both units remain available, labelled, for the per-dataset question.
    assert "raw /frame" in body
    assert "100000000" in body  # the raw score, in its own units, still shown


@pytest.mark.unit
def test_metrics_and_insights_pages_carry_the_shell() -> None:
    for html in (
        metrics_page({}, DEFAULT_THEME),
        insights_page({}, DEFAULT_THEME),
        episodes_page({"items": []}, None, None, DEFAULT_THEME),
    ):
        assert html.startswith("<!doctype html>")
        assert "de-nav" in html


@pytest.mark.unit
def test_episode_detail_renders_validation_results_and_violations() -> None:
    episode = {"id": "e-1", "metadata": {}, "state": "quarantined"}
    validations = [
        {
            "profile_name": "staged-strict",
            "profile_version": "1",
            "profile_hash": "a" * 64,
            "passed": False,
            "reason_codes": ["frame_count"],
            "violations": [{"code": "frame_count", "message": "too short"}],
            "created_at": "2026-09-29T00:00:00Z",
        }
    ]
    html = episode_detail_page(episode, None, DEFAULT_THEME, validations)
    assert "QUARANTINED" in html
    assert "staged-strict" in html
    assert "frame_count" in html
    assert "too short" in html

    passing = [{**validations[0], "passed": True, "reason_codes": [], "violations": []}]
    html = episode_detail_page(episode, None, DEFAULT_THEME, passing)
    assert "PASS" in html
    assert "no violations" in html

    empty = episode_detail_page(episode, None, DEFAULT_THEME, [])
    assert "no validation runs" in empty


@pytest.mark.unit
def test_job_detail_renders_produced_episodes_with_links() -> None:
    job = {"id": "j-1", "type": "ingest", "state": "succeeded"}
    produced = [
        {
            "id": "e-1",
            "episode_key": "episode_index=0",
            "format": "lerobot-v3",
            "state": "valid",
            "created_at": "2026-09-29T00:00:00Z",
        }
    ]
    html = job_detail_page(job, DEFAULT_THEME, produced)
    assert "Produced episodes" in html
    assert "/ui/episodes/e-1" in html
    assert "episode_index=0" in html

    empty = job_detail_page(job, DEFAULT_THEME, [])
    assert "has not registered episodes" in empty


@pytest.mark.unit
def test_job_detail_renders_the_run_report() -> None:
    job = {"id": "j-1", "type": "ingest", "state": "succeeded"}
    report = {
        "job": job,
        "episodes": {
            "total": 2,
            "by_state": {"valid": 1, "quarantined": 1},
            "verdicts": {"smooth": 1, "jerky": 1},
            "flags": {"jerky": 1, "stalled": 0},
            "length": {"count": 2},
            "mean_movement_score": 0.04,
            "mean_jerk_score": 0.01,
            "mean_stall_ratio": 0.1,
        },
        "validation": {
            "episodes_evaluated": 1,
            "results": 2,
            "passed": 1,
            "failed": 1,
            "reason_codes": {"TOO_FEW_FRAMES": 1},
        },
    }
    html = job_detail_page(job, DEFAULT_THEME, [], report)
    assert "Run report" in html
    assert "TOO_FEW_FRAMES &times; 1" in html
    assert "1 passed // 1 failed over 2 results" in html

    without = job_detail_page(job, DEFAULT_THEME, [])
    assert "Run report" not in without


def _incident(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "inc-1",
        "label": "CONTRACT_BREACH",
        "severity": "critical",
        "notify_class": "notify",
        "scope": "job-1",
        "summary": "Run produced 0 valid episodes; 5 were expected",
        "status": "open",
        "occurrence_count": 3,
        "last_seen": "2026-09-29T12:00:00Z",
    }
    row.update(overrides)
    return row


def _model(**overrides: object) -> dict[str, object]:
    model: dict[str, object] = {
        "items": [_incident()],
        "summary": {
            "by_status": {"open": 1},
            "by_severity": {"critical": 1},
            "by_label": {"CONTRACT_BREACH": 1},
            "notify_open": 1,
        },
        "health": {
            "last_tick_at": "2026-09-29T12:00:00+00:00",
            "baseline_scopes": 26,
            "warm_scopes": 12,
            "held_scopes": 1,
            "alert_budget_per_window": 10,
            "blind": False,
        },
        "digest": "1 need attention:\n  - [CONTRACT_BREACH] short",
    }
    model.update(overrides)
    return model


@pytest.mark.unit
def test_incidents_page_renders_the_queue_and_the_monitor_vitals() -> None:
    html = incidents_page(_model(), DEFAULT_THEME)
    assert "CONTRACT_BREACH" in html
    assert "Run produced 0 valid episodes" in html
    assert "Monitor health" in html
    assert "Notify preview" in html
    assert "INCIDENTS" in html.upper()


@pytest.mark.unit
def test_incidents_page_shows_occurrences_and_the_channel() -> None:
    html = incidents_page(_model(), DEFAULT_THEME)
    assert ">3<" in html
    assert "notify" in html


@pytest.mark.unit
def test_incidents_fragment_is_bare() -> None:
    fragment = incidents_fragment(_model())
    assert "CONTRACT_BREACH" in fragment
    assert "<!doctype html>" not in fragment


@pytest.mark.unit
def test_incidents_page_escapes_untrusted_incident_text() -> None:
    html = incidents_page(
        _model(items=[_incident(summary="<img src=x onerror=alert(1)>")]), DEFAULT_THEME
    )
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


@pytest.mark.unit
def test_incidents_page_escapes_the_scope() -> None:
    html = incidents_page(_model(items=[_incident(scope="<b>job</b>")]), DEFAULT_THEME)
    assert "<b>job</b>" not in html


@pytest.mark.unit
def test_a_blind_monitor_is_called_out_on_its_own_page() -> None:
    # A monitor that has silently stopped looks exactly like one with nothing to
    # report, and only one of those is healthy.
    health = dict(_model()["health"])  # type: ignore[arg-type]
    health["blind"] = True
    html = incidents_page(_model(health=health), DEFAULT_THEME)
    assert ">YES<" in html


@pytest.mark.unit
def test_incidents_page_survives_missing_health() -> None:
    html = incidents_page(_model(health={}), DEFAULT_THEME)
    assert "CONTRACT_BREACH" in html
    assert "Monitor health" not in html


@pytest.mark.unit
def test_incidents_page_survives_missing_summary() -> None:
    html = incidents_page(_model(summary={}), DEFAULT_THEME)
    assert "CONTRACT_BREACH" in html


@pytest.mark.unit
def test_incidents_page_polls_its_own_fragment() -> None:
    html = incidents_page(_model(), DEFAULT_THEME)
    assert 'data-poll="/ui/incidents"' in html
    assert "setInterval" not in html, "the poller lives in app.js, not inlined per page"


@pytest.mark.unit
def test_the_motion_trace_renders_a_literal_hole() -> None:
    """Two runs must draw as two lines: the gap is a gap, not a steep line."""
    episode = {
        "id": "ep-hole",
        "episode_key": "episode_0",
        "format": "lerobot-v3",
        "state": "ingested",
        "metadata": {"frame_count": 6},
    }
    quality = {
        "verdict": "smooth",
        "movement_score": 0.04,
        "jerk_score": 0.01,
        "stall_ratio": 0.1,
        "length_zscore": 0.0,
        "frame_count": 6,
        "dims": [],
        "motion_trace": [[[1.0, 0.2], [2.0, 0.3]], [[31.0, 0.4], [32.0, 0.5]]],
    }
    html = episode_detail_page(episode, quality, DEFAULT_THEME)
    assert html.count('<polyline class="de-trace"') == 2
    assert "recording dropouts, not stillness" in html

    continuous = dict(quality, motion_trace=[[[1.0, 0.2], [2.0, 0.3]]])
    html = episode_detail_page(episode, continuous, DEFAULT_THEME)
    assert html.count('<polyline class="de-trace"') == 1
    assert "continuous recording" in html
