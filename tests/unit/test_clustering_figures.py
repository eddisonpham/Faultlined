"""Tests for the cluster figures.

The figures cannot be eyeballed from a terminal, so every property a reader would
otherwise check by looking is asserted here instead: that treemap areas are
proportional to cluster sizes and do not overlap, that a convex hull really
encloses its cluster, that the projection accounts for a sensible share of variance,
and that each SVG is well-formed XML rather than a plausible-looking string.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import numpy as np
import pytest
from experiments.clustering import figures


def _parse(svg: str) -> ET.Element:
    """Parse, so a malformed attribute shows up as a test failure and not a viewer."""
    return ET.fromstring(svg)


# ------------------------------------------------------------------ treemap


def test_treemap_areas_are_proportional_to_sizes() -> None:
    sizes = [200.0, 50.0, 30.0, 10.0, 5.0, 1.0]
    width, height = 400.0, 300.0
    rects = figures.squarify(sizes, width, height)
    total = sum(sizes)
    for rect, size in zip(rects, sizes, strict=True):
        assert rect.area == pytest.approx(width * height * size / total, rel=1e-9)


def test_treemap_fills_the_canvas_exactly() -> None:
    width, height = 400.0, 300.0
    rects = figures.squarify([3.0, 1.0, 1.0, 4.0], width, height)
    assert sum(rect.area for rect in rects) == pytest.approx(width * height, rel=1e-9)


def test_treemap_rectangles_do_not_overlap() -> None:
    sizes = [17.0, 9.0, 4.0, 3.0, 2.0, 2.0, 1.0, 1.0]
    rects = figures.squarify(sizes, 500.0, 400.0)
    for i, first in enumerate(rects):
        for j, second in enumerate(rects[i + 1 :], start=i + 1):
            overlap_x = min(first.x + first.width, second.x + second.width) - max(first.x, second.x)
            overlap_y = min(first.y + first.height, second.y + second.height) - max(
                first.y, second.y
            )
            assert overlap_x <= 1e-6 or overlap_y <= 1e-6, f"{i} overlaps {j}"


def test_treemap_stays_inside_the_canvas() -> None:
    width, height = 300.0, 220.0
    for rect in figures.squarify([5.0, 3.0, 1.0, 8.0], width, height):
        assert rect.x >= -1e-9
        assert rect.y >= -1e-9
        assert rect.x + rect.width <= width + 1e-6
        assert rect.y + rect.height <= height + 1e-6


def test_treemap_keeps_rectangles_from_getting_absurdly_thin() -> None:
    """Squarified layout exists to avoid slivers; check it actually does.

    The bound is loose because the corpus is lopsided - a 200-to-1 size ratio cannot
    be laid out entirely square - but plain slice-and-dice passes an area check too
    while producing 200x1 rectangles for exactly this input, which is why this one
    is worth having.
    """
    rects = figures.squarify([200.0, 50.0, 30.0, 10.0, 5.0, 1.0], 600.0, 400.0)
    ratios = [max(r.width / r.height, r.height / r.width) for r in rects]
    assert max(ratios) < 10.0
    assert sum(ratios) / len(ratios) < 5.0


def test_treemap_returns_rectangles_in_the_order_it_was_given() -> None:
    """Duplicate sizes must not make two clusters indistinguishable.

    Matching results back to inputs by area looks simpler and silently labels every
    equal-sized cluster with the first one's name.
    """
    rects = figures.squarify([5.0, 5.0, 1.0], 200.0, 200.0)
    assert rects[0].area == pytest.approx(rects[1].area)
    assert rects[0].area > rects[2].area


def test_treemap_returns_one_rectangle_per_input_including_empty_clusters() -> None:
    """An empty cluster gets a zero-area rectangle rather than no rectangle.

    Dropping it would make the caller's `strict=True` zip raise, and an empty
    cluster is a real thing a clustering can produce.
    """
    rects = figures.squarify([4.0, 0.0, 6.0], 200.0, 100.0)
    assert len(rects) == 3
    assert rects[1].area == 0.0
    assert rects[0].area < rects[2].area


def test_treemap_rejects_an_empty_input() -> None:
    assert figures.squarify([], 10.0, 10.0) == []


def test_treemap_rejects_non_positive_area() -> None:
    with pytest.raises(ValueError, match="positive"):
        figures.squarify([1.0, -1.0], 10.0, 10.0)


# ----------------------------------------------------------------- geometry


def test_convex_hull_wraps_a_square_and_excludes_interior_points() -> None:
    points = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.5, 0.5], [0.5, 0.1]])
    hull = figures.convex_hull(points)
    assert len(hull) == 4
    for interior in ([0.5, 0.5], [0.5, 0.1]):
        assert not any(np.allclose(interior, vertex) for vertex in hull)


def test_convex_hull_of_collinear_points_has_no_area() -> None:
    points = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]])
    hull = figures.convex_hull(points)
    assert len(hull) <= 2


def test_convex_hull_passes_through_degenerate_inputs() -> None:
    assert len(figures.convex_hull(np.array([[1.0, 1.0]]))) == 1
    assert len(figures.convex_hull(np.array([[1.0, 1.0], [2.0, 2.0]]))) == 2


def test_every_hull_point_is_a_real_cluster_point() -> None:
    rng = np.random.default_rng(1)
    points = rng.normal(size=(30, 2))
    hull = figures.convex_hull(points)
    for vertex in hull:
        assert any(np.allclose(vertex, point) for point in points)


def test_projection_explains_at_most_all_of_the_variance() -> None:
    rng = np.random.default_rng(2)
    _coords, explained = figures.project_2d(rng.normal(size=(40, 6)))
    assert 0.0 <= explained[0] <= 1.0
    assert 0.0 <= explained[1] <= 1.0
    assert explained[0] + explained[1] <= 1.0 + 1e-9


def test_projection_keeps_the_dominant_axis_first() -> None:
    """Variance along x is far larger, so axis 1 must report it."""
    rng = np.random.default_rng(3)
    points = np.column_stack([rng.normal(size=50) * 8.0, rng.normal(size=50)])
    _coords, explained = figures.project_2d(points)
    assert explained[0] > 0.9


def test_projection_needs_two_points() -> None:
    with pytest.raises(ValueError, match="at least two"):
        figures.project_2d(np.ones((1, 4)))


# ----------------------------------------------------------------- heatmap


def test_heatmap_rows_are_normalised_across_tags() -> None:
    """A big cluster must not outrank a small one purely by size."""
    members = {0: ["a", "b", "c"], 1: ["d"]}
    tags = {"a": ["x"], "b": ["x"], "c": ["y"], "d": ["y"]}
    matrix = figures.cluster_attribute_matrix(members, tags, ["x", "y"])
    assert matrix[0][0] == pytest.approx(2 / 3)
    assert matrix[0][1] == pytest.approx(1 / 3)
    assert matrix[1][0] == pytest.approx(0.0)
    assert matrix[1][1] == pytest.approx(1.0)


def test_heatmap_ignores_tags_that_are_not_columns() -> None:
    members = {0: ["a"]}
    tags = {"a": ["x", "not-a-column"]}
    matrix = figures.cluster_attribute_matrix(members, tags, ["x"])
    assert matrix[0][0] == pytest.approx(1.0)


def test_heatmap_survives_an_empty_cluster() -> None:
    matrix = figures.cluster_attribute_matrix({0: []}, {}, ["x"])
    assert matrix.shape == (1, 1)
    assert matrix[0][0] == 0.0


def test_heatmap_rejects_a_mismatched_matrix() -> None:
    with pytest.raises(ValueError, match="does not match"):
        figures.render_heatmap(np.zeros((2, 2)), ["a"], ["x", "y", "z"], "title")


# ------------------------------------------------------------------ output


def test_every_figure_is_well_formed_svg() -> None:
    size = 640.0, 480.0
    sizes = [10, 5, 5, 1]
    labels = ["c0", "c1", "c2", "c3"]
    vectors = np.random.default_rng(4).normal(size=(12, 5))
    labels_int = [0, 0, 0, 1, 1, 1, 2, 2, 3, 3, 0, 1]

    rendered = [
        figures.render_treemap(sizes, labels, "treemap"),
        figures.render_map(vectors, labels_int, "map"),
        figures.render_heatmap(np.ones((4, 3)) * 0.5, labels, ["x", "y", "z"], "heatmap"),
    ]
    for svg in rendered:
        root = _parse(svg)
        assert root.tag.endswith("svg")
        assert root.get("viewBox")
        _ = size


def test_treemap_draws_one_rectangle_per_cluster() -> None:
    sizes = [4, 3, 2, 1]
    root = _parse(figures.render_treemap(sizes, ["a", "b", "c", "d"], "treemap"))
    assert len(root.findall("{http://www.w3.org/2000/svg}rect")) == len(sizes) + 1


def test_map_draws_one_point_per_string_and_a_hull_per_sized_cluster() -> None:
    vectors = np.random.default_rng(5).normal(size=(9, 4))
    labels = [0, 0, 0, 1, 1, 1, 2, 3, 4]
    root = _parse(figures.render_map(vectors, labels, "map"))
    namespace = "{http://www.w3.org/2000/svg}"
    assert len(root.findall(f"{namespace}circle")) == len(labels)
    # Only the two clusters with three members have a hull.
    assert len(root.findall(f"{namespace}polygon")) == 2


def test_titles_are_escaped_rather_than_injected() -> None:
    svg = figures.render_treemap([1, 1], ["a", "b"], '<script>alert("x")</script>')
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    _parse(svg)


def test_empty_colour_lists_do_not_break_the_ramp() -> None:
    assert figures._ramp(-1.0) == figures._ramp(0.0)
    assert figures._ramp(2.0) == figures._ramp(1.0)


def test_cluster_hues_are_deterministic_and_distinct() -> None:
    assert figures._cluster_hue(3) == figures._cluster_hue(3)
    assert len({figures._cluster_hue(index) for index in range(12)}) == 12
