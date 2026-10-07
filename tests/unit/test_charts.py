"""Chart primitives: the assertions are coordinates, not screenshots (ADR 0024)."""

from __future__ import annotations

import math
import re

import pytest

from data_engine.web.charts import (
    DECADES_BEFORE_LOG,
    bar_chart,
    choose_scale,
    format_value,
    line_chart,
    polyline_points,
)

pytestmark = pytest.mark.unit


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (0.0, "0"),
        (0.5, "0.500"),
        (2.0, "2"),
        (2.5, "2.50"),
        (42.0, "42"),
        (1234.0, "1234"),
        (0.0004, "4.0e-04"),
        (1.0e8, "1.0e+08"),
        (math.inf, "inf"),
        (-math.inf, "-inf"),
    ],
)
def test_a_tick_label_fits_its_tick(value: float, shown: str) -> None:
    """`100015978.527` on an axis is an axis nobody reads."""
    assert format_value(value) == shown


@pytest.mark.unit
def test_a_narrow_series_stays_linear() -> None:
    scale = choose_scale([1.0, 2.0, 3.0])
    assert not scale.logarithmic
    assert (scale.lo, scale.hi) == (1.0, 3.0)


@pytest.mark.unit
def test_a_series_spanning_four_decades_goes_logarithmic_on_its_own() -> None:
    """The reference run's raw speeds span 0.022 to 1.0e8."""
    scale = choose_scale([0.022, 3.54, 2.31, 0.16, 1.0e8])
    assert scale.logarithmic
    assert math.log10(scale.hi / scale.lo) > DECADES_BEFORE_LOG


@pytest.mark.unit
def test_a_series_containing_zero_stays_linear_because_it_cannot_be_logged() -> None:
    scale = choose_scale([0.0, 1.0e8])
    assert not scale.logarithmic
    assert scale.lo == 0.0


@pytest.mark.unit
def test_a_negative_series_stays_linear() -> None:
    assert not choose_scale([-1.0e8, -1.0, 5.0]).logarithmic


@pytest.mark.unit
def test_one_non_finite_sample_does_not_flatten_the_whole_axis() -> None:
    """ADR 0023 removed this failure from the metrics; the chart inherits the rule."""
    scale = choose_scale([1.0, 2.0, math.nan, 4.0, math.inf])
    assert (scale.lo, scale.hi) == (1.0, 4.0)
    assert not scale.logarithmic


@pytest.mark.unit
def test_an_empty_series_gets_a_drawable_band_instead_of_dividing_by_zero() -> None:
    assert (choose_scale([]).lo, choose_scale([]).hi) == (0.0, 1.0)


@pytest.mark.unit
def test_a_flat_series_gets_a_band_so_the_trace_renders_in_the_middle() -> None:
    scale = choose_scale([7.0, 7.0, 7.0])
    assert scale.lo < 7.0 < scale.hi


@pytest.mark.unit
def test_a_log_scale_keeps_its_ordering_across_the_domain() -> None:
    scale = choose_scale([1.0, 1.0e6], decades=1.0)
    assert scale.pixel(1.0, 100.0, 0.0) == pytest.approx(100.0)
    assert scale.pixel(1.0e6, 100.0, 0.0) == pytest.approx(0.0)
    assert scale.pixel(1.0e3, 100.0, 0.0) == pytest.approx(50.0)


@pytest.mark.unit
def test_a_gap_breaks_the_trace_instead_of_connecting_across_it() -> None:
    """Connecting a dropout draws motion through a hole in the recording."""
    samples: list[tuple[float, float | None]] = [
        (0, 1.0),
        (1, 2.0),
        (2, None),
        (3, None),
        (4, 5.0),
        (5, 6.0),
    ]
    scale = choose_scale([1.0, 2.0, 5.0, 6.0])
    runs = polyline_points(samples, scale, x0=0.0, x1=100.0, top=0.0, bottom=100.0)

    assert len(runs) == 2
    assert [round(x) for x, _ in runs[0]] == [0, 20]
    assert [round(x) for x, _ in runs[1]] == [80, 100]


@pytest.mark.unit
def test_a_leading_or_trailing_gap_does_not_invent_a_run() -> None:
    samples: list[tuple[float, float | None]] = [(0, None), (1, 1.0), (2, 2.0), (3, None)]
    runs = polyline_points(samples, choose_scale([1.0, 2.0]), x0=0.0, x1=30.0, top=0.0, bottom=10.0)
    assert len(runs) == 1
    assert len(runs[0]) == 2


@pytest.mark.unit
def test_a_single_surviving_point_is_kept_so_a_sparse_recording_is_still_visible() -> None:
    samples: list[tuple[float, float | None]] = [(0, None), (1, None), (2, 4.0), (3, None)]
    runs = polyline_points(samples, choose_scale([4.0]), x0=0.0, x1=30.0, top=0.0, bottom=10.0)
    assert len(runs) == 1 and len(runs[0]) == 1


@pytest.mark.unit
def test_a_line_chart_states_its_scale_in_the_accessible_label() -> None:
    svg = line_chart([(i, 10.0**i) for i in range(9)], label="throughput")
    assert 'role="img"' in svg
    assert "log10" in svg
    assert "throughput" in svg


@pytest.mark.unit
def test_a_line_chart_renders_one_polyline_per_contiguous_run() -> None:
    svg = line_chart([(0, 1.0), (1, 2.0), (2, None), (3, 4.0), (4, 5.0)], label="gaps")
    assert svg.count("<polyline") == 2


@pytest.mark.unit
def test_a_line_chart_draws_a_visible_trace_when_there_is_data() -> None:
    svg = line_chart([(i, float(i)) for i in range(5)], label="trend")
    assert "<polyline" in svg
    assert "no data" not in svg


@pytest.mark.unit
def test_a_line_chart_with_no_samples_says_so_instead_of_rendering_an_empty_frame() -> None:
    """An empty frame with no explanation reads as a broken page."""
    svg = line_chart([], label="latency", caption="no jobs have run yet")
    assert "no data" in svg
    assert "no jobs have run yet" in svg
    assert "<polyline" not in svg


@pytest.mark.unit
def test_a_line_chart_keeps_its_x_tick_labels() -> None:
    svg = line_chart(
        [(i, float(i)) for i in range(4)],
        x_ticks=[(10.0, "09:00"), (300.0, "09:05")],
    )
    assert "09:00" in svg and "09:05" in svg


@pytest.mark.unit
def test_chart_text_is_escaped_so_a_metric_name_cannot_inject_markup() -> None:
    svg = line_chart([(0, 1.0), (1, 2.0)], label="<script>alert(1)</script>")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


@pytest.mark.unit
def test_a_chart_with_every_value_identical_still_renders() -> None:
    svg = line_chart([(i, 3.0) for i in range(4)], label="steady")
    assert "<polyline" in svg


@pytest.mark.unit
def test_a_bar_chart_never_goes_logarithmic() -> None:
    """A log axis has no meaning when the quantity being counted is a population."""
    svg = bar_chart([("a", 1.0), ("b", 1.0e6)], label="counts")
    assert "log10" not in svg


@pytest.mark.unit
def test_a_bar_chart_labels_every_bucket_when_there_are_few() -> None:
    svg = bar_chart([("ok", 3), ("bad", 1)], label="verdicts")
    assert ">ok<" in svg and ">bad<" in svg


@pytest.mark.unit
def test_a_bar_chart_thins_its_labels_rather_than_overprinting_them() -> None:
    names = [f"n{i}" for i in range(20)]
    svg = bar_chart([(name, 1) for name in names], label="many")
    assert svg.count("de-tick") < 20


@pytest.mark.unit
def test_a_bar_chart_carries_its_value_in_a_title_for_each_bar() -> None:
    svg = bar_chart([("gapped", 2)], label="integrity")
    assert "<title>gapped: 2</title>" in svg


@pytest.mark.unit
def test_an_empty_bar_chart_says_no_data() -> None:
    assert "no data" in bar_chart([], label="verdicts")


@pytest.mark.unit
def test_every_chart_opens_and_closes_exactly_one_svg() -> None:
    for svg in (
        line_chart([(0, 1.0), (1, 2.0)], label="a"),
        line_chart([], label="b"),
        bar_chart([("x", 1)], label="c"),
        bar_chart([], label="d"),
    ):
        assert svg.count("<svg") == 1, svg[:80]
        assert svg.count("</svg>") == 1, svg[:80]


@pytest.mark.unit
def test_every_chart_is_labelled_for_a_screen_reader() -> None:
    for svg in (line_chart([(0, 1.0)], label="a"), bar_chart([("x", 1)], label="b")):
        assert re.search(r'aria-label="[^"]{4,}"', svg), svg[:120]


@pytest.mark.unit
def test_a_caption_is_escaped_and_its_link_is_not() -> None:
    """One escaped argument cannot carry a link; one unescaped is a hole."""
    svg = line_chart(
        [(0, 1.0), (1, 2.0)],
        label="latency",
        caption="peak 0.4 s <not a tag>",
        link=("drill down", "/ui/jobs"),
    )
    assert "&lt;not a tag&gt;" in svg
    assert '<a href="/ui/jobs">drill down</a>' in svg
    assert "&lt;a href" not in svg


@pytest.mark.unit
def test_a_chart_with_neither_caption_nor_link_emits_no_note() -> None:
    assert "de-chart-note" not in line_chart([(0, 1.0), (1, 2.0)], label="x")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("x", "anchor"),
    [(0.0, "start"), (10.0, "start"), (200.0, "middle"), (600.0, "middle"), (710.0, "end")],
)
def test_a_tick_label_hangs_from_the_end_nearest_the_frame(x: float, anchor: str) -> None:
    from data_engine.web.charts import PAD_LEFT, PAD_RIGHT, _anchor

    assert _anchor(x, PAD_LEFT, 720 - PAD_RIGHT) == anchor


@pytest.mark.unit
def test_the_first_and_last_x_labels_do_not_hang_past_the_frame() -> None:
    """A centred label at the right edge loses its last characters."""
    svg = line_chart(
        [(i, float(i)) for i in range(10)],
        x_ticks=[(52.0, "04:20"), (381.0, "04:21"), (710.0, "04:22")],
    )
    assert 'text-anchor="start">04:20<' in svg
    assert 'text-anchor="end">04:22<' in svg
    assert 'text-anchor="middle">04:21<' in svg
    assert "04:22" in svg


def test_x_is_mapped_by_value_not_by_index() -> None:
    from data_engine.web.charts import choose_scale, polyline_points

    runs = polyline_points(
        [(0.0, 0.5), (50.0, 0.5), (100.0, 0.5)],
        choose_scale([0.5]),
        x0=0.0,
        x1=100.0,
        top=0.0,
        bottom=0.0,
    )
    assert [x for x, _ in runs[0]] == [0.0, 50.0, 100.0]


def test_a_hole_keeps_its_measured_width() -> None:
    from data_engine.web.charts import choose_scale, polyline_points

    samples = [
        (0.0, 0.5),
        (1.0, 0.5),
        (31.0, None),
        (31.0, 0.5),
        (32.0, 0.5),
    ]
    runs = polyline_points(samples, choose_scale([0.5]), x0=0.0, x1=32.0, top=0.0, bottom=0.0)
    assert len(runs) == 2
    assert runs[1][0][0] - runs[0][-1][0] == pytest.approx(30.0)


def test_indexed_input_maps_pixel_for_pixel_as_it_always_did() -> None:
    from data_engine.web.charts import choose_scale, polyline_points

    runs = polyline_points(
        list(enumerate([1.0, 2.0, 3.0])),
        choose_scale([1.0, 3.0]),
        x0=10.0,
        x1=110.0,
        top=0.0,
        bottom=0.0,
    )
    assert [x for x, _ in runs[0]] == [10.0, 60.0, 110.0]
