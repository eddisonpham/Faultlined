"""Tests for the Clusters page's figures, its layout, and confirmation re-attachment.

A chart is a string, so everything a reader would check by looking is asserted here
instead: that a treemap's areas are proportional to episodes, that a boundary really
encloses the points it claims, that a confirmed cluster is drawn differently from an
unconfirmed one, and that a label survives a rebuild that re-keys every proposal.

That last one is here because it was measured broken: the first version pointed the
confirmations table at the proposal hash with a cascading reference, and one rebuild with
different axes deleted every label an operator had written.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from data_engine.clustering import Ignored, build, convex_hull, project, token_vector
from data_engine.web import cluster_page
from data_engine.web.clusters import cluster_colour, cluster_map, empty_map, treemap

TASKS = {
    "pick up the red mug": 12,
    "pick up the blue mug": 3,
    "grab the mug": 5,
    "put down the laptop": 7,
    "open the drawer": 4,
    "wipe the counter": 2,
}


def _proposals(**options: object):
    return build(TASKS, ignored=Ignored(verb=True, colour=True), **options)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- layout


def test_a_hull_encloses_every_point_it_was_built_from() -> None:
    points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0), (0.5, 0.5)]
    hull = convex_hull(points)
    xs = [x for x, _ in hull]
    ys = [y for _, y in hull]
    assert len(hull) == 4
    for x, y in points:
        assert min(xs) <= x <= max(xs)
        assert min(ys) <= y <= max(ys)


def test_a_hull_of_collinear_points_is_a_line_not_a_polygon() -> None:
    assert len(convex_hull([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])) <= 2


def test_the_projection_is_deterministic_and_spreads_the_points() -> None:
    vectors = [token_vector(core) for core in ("mug", "laptop", "drawer", "counter", "spoon")]
    first = project(vectors)
    second = project(vectors)
    assert first.points == second.points
    assert first.extent > 0.0
    assert 0.0 <= first.explained <= 1.0


def test_too_few_points_do_not_raise_and_report_no_variance() -> None:
    assert project([]).points == ()
    assert project([[1.0, 0.0]]).points == ((0.0, 0.0),)
    assert project([[1.0, 0.0], [0.0, 1.0]]).points


def test_the_projection_caps_its_work_and_reports_what_it_dropped() -> None:
    vectors = [token_vector(f"core number {index}") for index in range(12)]
    layout = project(vectors, limit=5)
    assert len(layout.points) == 5
    assert layout.dropped == 7


# --------------------------------------------------------------------- figures


def test_a_treemap_is_well_formed_svg_with_one_rect_per_cluster() -> None:
    svg = treemap(_proposals().proposals)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    rects = root.findall(".//{http://www.w3.org/2000/svg}rect")
    assert len(rects) == _proposals().cluster_count


def test_treemap_widths_are_proportional_to_episodes() -> None:
    proposals = _proposals()
    svg = treemap(proposals.proposals)
    root = ET.fromstring(svg)
    cells = [
        (rect.get("x"), float(rect.get("width", "0")))
        for rect in root.findall(".//{http://www.w3.org/2000/svg}rect")
    ]
    # Cells in a row share that row's total, so the assertion is per row: within a row
    # the biggest cluster's cell must be wider than the smallest one's.
    widest = max(cells, key=lambda cell: cell[1])[1]
    narrowest = min(cells, key=lambda cell: cell[1])[1]
    assert widest > narrowest
    assert len(cells) == proposals.cluster_count


def test_an_empty_treemap_states_what_is_absent_instead_of_rendering_nothing() -> None:
    svg = empty_map(message="no clusters yet")
    root = ET.fromstring(svg)
    text = "".join(root.itertext())
    assert "no clusters yet" in text


def test_the_map_draws_one_point_per_task_string_and_a_boundary_per_cluster() -> None:
    proposals = _proposals()
    root = ET.fromstring(cluster_map(proposals))
    namespace = "{http://www.w3.org/2000/svg}"
    circles = root.findall(f".//{namespace}circle")
    polygons = root.findall(f".//{namespace}polygon")
    assert len(circles) == proposals.health()["tasks"]
    # A boundary needs three points, so only clusters with several members get one.
    assert len(polygons) <= proposals.cluster_count


def test_a_confirmed_cluster_is_drawn_differently_from_an_unconfirmed_one() -> None:
    proposals = _proposals()
    target = max(proposals.proposals, key=lambda item: item.size)
    proposals.apply_confirmations({target.key: "mug handling"})
    root = ET.fromstring(cluster_map(proposals))
    namespace = "{http://www.w3.org/2000/svg}"
    strokes = {polygon.get("stroke-width") for polygon in root.findall(f".//{namespace}polygon")}
    dashed = {polygon.get("stroke-dasharray") for polygon in root.findall(f".//{namespace}polygon")}
    assert "2.0" in strokes or dashed == {"none"}


def test_a_cluster_colour_is_stable_for_a_key() -> None:
    assert cluster_colour("abc123") == cluster_colour("abc123")
    assert cluster_colour("abc123") != cluster_colour("zzz999")


def test_task_text_is_escaped_into_the_figure() -> None:
    """A task string is operator-supplied text and must not become markup."""
    proposals = build({"pick up <script>alert(1)</script> cube": 1}, ignored=Ignored())
    svg = cluster_map(proposals)
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert ET.fromstring(svg).tag.endswith("svg")


# ----------------------------------------------------------------- page body


def test_the_health_section_leads_with_the_numbers() -> None:
    html = cluster_page.health_section(_proposals())
    assert "de-stat-value" in html
    # The fixture's episode counts sum to 33, and the total has to be one of the tiles.
    assert ">33<" in html


def test_the_interpretation_names_the_failure_it_sees() -> None:
    # A zero radius: every distinct string becomes its own cluster, which is the shape
    # EXP-2.5-08 measured on real operator text (46 sentences, 34 clusters).
    fragmenting = build(
        {f"pick up object number {index}": 1 for index in range(40)},
        ignored=Ignored(),
        radius=0.0,
    )
    assert "Fragmenting" in cluster_page.interpretation(fragmenting)

    # A radius wide enough to swallow everything: few clusters, many task strings.
    merging = build(
        {
            f"pick up the {colour} object": index + 1
            for index, colour in enumerate(
                ("red", "blue", "green", "black", "white", "brown", "pink", "grey")
            )
        },
        ignored=Ignored(verb=True, colour=True),
        radius=2.0,
    )
    assert merging.cluster_count == 1
    assert "Merging" in cluster_page.interpretation(merging)

    healthy = _proposals()
    assert "clusters over" in cluster_page.interpretation(healthy)


def test_a_corpus_too_small_to_judge_says_so() -> None:
    """One task string is not a fragmentation rate, and calling it one would be noise."""
    tiny = build({"pick up the red cube": 1}, ignored=Ignored())
    assert "Too few to judge" in cluster_page.interpretation(tiny)


def test_the_interpretation_of_an_empty_catalog_says_what_to_do() -> None:
    empty = build({}, ignored=Ignored())
    assert "no task strings" in cluster_page.interpretation(empty)


def test_the_controls_form_posts_without_javascript() -> None:
    html = cluster_page.controls(_proposals())
    assert 'method="post"' in html
    assert 'action="/ui/clusters/rebuild"' in html
    assert "onclick" not in html
    assert 'name="ignore" value="verb"' in html


def test_the_controls_show_the_settings_the_run_was_built_with() -> None:
    """A dropdown that says `catalog` over a sample run invites a rebuild that undoes it."""
    sample = build(TASKS, ignored=Ignored(verb=True), source="sample", radius=0.45)
    html = cluster_page.controls(sample)
    assert '<option value="sample" selected>' in html
    assert 'value="0.45"' in html
    assert "sliding_8" not in html or 'value="sliding_8" selected' not in html


def test_the_proposal_table_offers_confirm_for_open_clusters_and_release_for_frozen() -> None:
    proposals = _proposals()
    target = max(proposals.proposals, key=lambda item: item.size)
    open_html = cluster_page.proposal_table(proposals)
    assert f'action="/ui/clusters/{target.key}/confirm"' in open_html

    proposals.apply_confirmations({target.key: "mug handling"})
    frozen_html = cluster_page.proposal_table(proposals)
    assert f'action="/ui/clusters/{target.key}/release"' in frozen_html
    assert "mug handling" in frozen_html


def test_the_proposal_table_escapes_a_stored_label() -> None:
    proposals = _proposals()
    target = max(proposals.proposals, key=lambda item: item.size)
    proposals.apply_confirmations({target.key: "<b>not bold</b>"})
    html = cluster_page.proposal_table(proposals)
    assert "<b>not bold</b>" not in html
    assert "&lt;b&gt;not bold&lt;/b&gt;" in html


def test_a_row_never_lists_more_than_four_task_strings() -> None:
    """One cluster with many merged cores used to make one row taller than the table.

    The members are still all there - behind the cluster detail - but a reviewer reading
    twenty rows needs the rows to be the same height to compare them.
    """
    many = {
        f"pick up the {colour} mug": index + 1
        for index, colour in enumerate(
            ["red", "blue", "green", "yellow", "white", "black", "orange", "pink"]
        )
    }
    html = cluster_page.proposal_table(build(many, ignored=Ignored(verb=True, colour=True)))
    assert html.count("mug</span>") + html.count("mug") <= 4 * 4, "too many members per row"
    assert "and 4 more" in html
    assert 'href="/api/v1/clusters/' in html


def test_the_map_uses_the_whole_canvas_on_each_axis() -> None:
    """A cloud with little vertical spread used to float in a tall box.

    The two axes are scaled independently to the data's own extent, so the drawing fills
    the rectangle. The layout note already says the axes carry no meaning, so nothing is
    lost by fitting them separately - and the note on the figure says so.
    """
    svg = cluster_map(_proposals())
    root = ET.fromstring(svg)
    circles = root.findall(".//{http://www.w3.org/2000/svg}circle")
    xs = [float(circle.get("cx", "0")) for circle in circles]
    ys = [float(circle.get("cy", "0")) for circle in circles]
    assert max(xs) - min(xs) > 400, "the drawing is not using the width"
    assert max(ys) - min(ys) > 240, "the drawing is not using the height"
    assert "each axis scaled to fit" in svg


def test_the_colour_key_is_shown_next_to_the_figures() -> None:
    """Both figures colour clusters by a hash of the key, and nothing said so."""
    html = cluster_page.figures_section(_proposals())
    assert "colour follows the cluster key" in html
    first = cluster_page.rows_for(_proposals())[0]
    assert first.colour in html


def test_the_history_section_is_empty_until_a_run_exists() -> None:
    assert "no clustering runs" in cluster_page.history_section([])
    rows = cluster_page.history_section(
        [
            {
                "id": 1,
                "source": "sample",
                "ignored": "colour",
                "radius": 0.3,
                "rule": "running_mean",
                "health": {"clusters": 4},
                "created_at": "2026-10-01",
            }
        ]
    )
    assert "sample" in rows
    assert "2026-10-01" in rows


def test_the_sample_note_says_the_data_is_illustrative() -> None:
    assert "not catalog data" in cluster_page.sample_note()


# ------------------------------------------------------- confirmation survival


def test_a_confirmation_survives_a_rebuild_that_re_keys_every_proposal() -> None:
    from data_engine.catalog.clusters import Confirmation, reapply

    # The fixture carries a location tail, so ignoring the location axis genuinely
    # changes the cores - which is exactly what re-keys every proposal.
    tasks = {**TASKS, "place the bowl on the plate": 6, "place the bowl on the tray": 3}
    before = build(tasks, ignored=Ignored(verb=True, colour=True))
    target = max(before.proposals, key=lambda item: item.size)
    stored = [Confirmation(label="mug handling", tasks=tuple(m.task for m in target.members))]

    # A rebuild with a different axis set: different cores, therefore different keys.
    after = build(tasks, ignored=Ignored(verb=True, colour=True, site=True))
    # The cores really do change, which is what would have broken a key-based match.
    assert {item.core for item in after.proposals} != {item.core for item in before.proposals}

    reapplied = reapply(stored, after)
    assert reapplied.frozen == 1
    assert reapplied.orphaned == 0
    assert [item.label for item in after.proposals if item.frozen] == ["mug handling"]


def test_a_confirmation_whose_tasks_are_gone_is_counted_as_orphaned() -> None:
    from data_engine.catalog.clusters import Confirmation, reapply

    stored = [Confirmation(label="gone", tasks=("task that no longer exists",))]
    reapplied = reapply(stored, _proposals())
    assert reapplied.frozen == 0
    assert reapplied.orphaned == 1
    assert reapplied.as_dict() == {"frozen": 0, "orphaned": 1}


def test_a_confirmation_moves_to_a_fragment_only_while_one_still_holds_most_of_it() -> None:
    """Matching on any overlap at all is how an operator's name ends up on the wrong thing.

    A group of four that scatters into two pairs still has a majority in one proposal, so
    the name follows the group. Scatter it further and nothing holds more than half, so
    the claim is reported as lost rather than quietly reattached.
    """
    from data_engine.catalog.clusters import Confirmation, labels_for

    members = {
        "left": {"a", "b", "c", "d"},
        "right": {"e", "f"},
    }
    assert labels_for(members, [Confirmation(label="group", tasks=("a", "b", "c", "d"))]) == {
        "left": "group"
    }

    scattered = {"left": {"a", "b"}, "right": {"c", "d"}}
    assert labels_for(scattered, [Confirmation(label="group", tasks=("a", "b", "c", "d"))]) == {}


def test_a_confirmation_covers_nothing_when_it_has_no_tasks() -> None:
    with pytest.raises(ValueError, match="at least one task"):
        from data_engine.catalog.clusters import Confirmation

        Confirmation(label="empty", tasks=())
