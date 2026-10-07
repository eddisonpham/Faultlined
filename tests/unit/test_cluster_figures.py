"""Tests for the Clusters page's ranked volume comparison and review workflow."""

from __future__ import annotations

import re

import pytest

from data_engine.clustering import Ignored, build
from data_engine.web import cluster_page
from data_engine.web.clusters import class_size_view, cluster_colour

TASKS = {
    "pick up the red mug": 12,
    "pick up the blue mug": 3,
    "grab the mug": 5,
    "put down the laptop": 7,
    "open the drawer": 4,
    "wipe the counter": 2,
}


def _proposals():
    return build(TASKS, ignored=Ignored(verb=True, colour=True))


def test_class_size_view_is_ranked_fluid_and_reports_exact_values() -> None:
    proposals = _proposals()
    html = class_size_view(proposals.ordered())
    assert 'class="de-class-size"' in html
    assert "width:100%" not in html
    assert "episodes</span>" in html
    assert "distinct task strings" in html
    assert "of clustered episodes" in html
    assert "33" in html
    assert "100.000%" in html
    assert "<svg" not in html
    assert "<circle" not in html


def test_class_size_order_and_bar_lengths_follow_episode_volume() -> None:
    proposals = _proposals()
    html = class_size_view(proposals.proposals)
    largest = max(proposals.proposals, key=lambda item: item.size)
    assert f"<strong>{largest.core}</strong>" in html
    widths = [float(value) for value in re.findall(r"--de-class-width:([0-9.]+)%", html)]
    assert widths
    assert max(widths) == pytest.approx(100.0)
    assert widths == sorted(widths, reverse=True)


def test_class_size_view_handles_empty_data() -> None:
    assert "no classes to compare" in class_size_view(())


def test_class_size_view_escapes_operator_text() -> None:
    proposals = build({"open the drawer": 1}, ignored=Ignored())
    proposals.apply_confirmations({proposals.proposals[0].key: "<script>alert(1)</script>"})
    html = class_size_view(proposals.proposals)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_health_leads_with_the_counts_and_explains_fragmentation() -> None:
    html = cluster_page.health_section(_proposals())
    assert "de-stat-value" in html
    assert ">33<" in html

    fragmenting = build(
        {f"pick up object number {index}": 1 for index in range(40)},
        ignored=Ignored(),
        radius=0.0,
    )
    assert "Fragmenting" in cluster_page.interpretation(fragmenting)


def test_small_and_empty_corpora_are_not_overinterpreted() -> None:
    assert "Too few to judge" in cluster_page.interpretation(
        build({"pick up the red cube": 1}, ignored=Ignored())
    )
    assert "no task strings" in cluster_page.interpretation(build({}, ignored=Ignored()))


def test_rebuild_controls_are_plain_posts_and_reflect_the_run() -> None:
    sample = build(TASKS, ignored=Ignored(verb=True), source="sample", radius=0.45)
    html = cluster_page.controls(sample)
    assert 'method="post"' in html
    assert 'action="/ui/clusters/rebuild"' in html
    assert 'name="ignore" value="verb"' in html
    assert '<option value="sample" selected>' in html
    assert 'value="0.45"' in html
    assert "onclick" not in html


def test_review_queue_offers_human_class_or_dismissal_with_honest_evidence() -> None:
    candidate = {
        "task": "<place the cup>",
        "reason": "no supporting neighbors",
        "episodes": 2,
        "distance": 0.4,
        "radius": 0.3,
        "proposal_core": "cup",
    }
    html = cluster_page.review_section([candidate], [])
    assert 'action="/ui/clusters/review"' in html
    assert 'value="class">create human class' in html
    assert 'value="dismissed">' in html
    assert "Distance is a review heuristic, not confidence" in html
    assert "<place" not in html
    assert "&lt;place" in html
    assert "Select up to 25" in html


def test_review_history_can_reopen_a_prior_disposition() -> None:
    html = cluster_page.review_section(
        [], [{"task": "open drawer", "disposition": "dismissed", "label": ""}]
    )
    assert "Recent human decisions" in html
    assert 'action="/ui/clusters/review/undo"' in html
    assert ">reopen</button>" in html


def test_empty_review_queue_has_an_explicit_state() -> None:
    assert "no unresolved singleton or near-boundary" in cluster_page.review_section([], [])


def test_proposal_table_offers_confirm_for_open_clusters_and_release_for_frozen() -> None:
    proposals = _proposals()
    target = max(proposals.proposals, key=lambda item: item.size)
    open_html = cluster_page.proposal_table(proposals)
    assert f'action="/ui/clusters/{target.key}/confirm"' in open_html

    proposals.apply_confirmations({target.key: "mug handling"})
    frozen_html = cluster_page.proposal_table(proposals)
    assert f'action="/ui/clusters/{target.key}/release"' in frozen_html
    assert "mug handling" in frozen_html


def test_proposal_table_escapes_labels_and_bounds_visible_members() -> None:
    proposals = _proposals()
    target = max(proposals.proposals, key=lambda item: item.size)
    proposals.apply_confirmations({target.key: "<b>not bold</b>"})
    html = cluster_page.proposal_table(proposals)
    assert "<b>not bold</b>" not in html
    assert "&lt;b&gt;not bold&lt;/b&gt;" in html

    many = {
        f"pick up the {colour} mug": index + 1
        for index, colour in enumerate(
            ["red", "blue", "green", "yellow", "white", "black", "orange", "pink"]
        )
    }
    compact = cluster_page.proposal_table(build(many, ignored=Ignored(verb=True, colour=True)))
    assert "and 4 more" in compact
    assert 'href="/ui/clusters/' in compact


def test_cluster_colour_is_stable_for_a_key() -> None:
    assert cluster_colour("abc123") == cluster_colour("abc123")
    assert cluster_colour("abc123") != cluster_colour("zzz999")


def test_confirmation_survives_rekeying_only_while_a_majority_stays_together() -> None:
    from data_engine.catalog.clusters import Confirmation, labels_for, reapply

    tasks = {**TASKS, "place the bowl on the plate": 6, "place the bowl on the tray": 3}
    before = build(tasks, ignored=Ignored(verb=True, colour=True))
    target = max(before.proposals, key=lambda item: item.size)
    stored = [Confirmation(label="mug handling", tasks=tuple(m.task for m in target.members))]
    after = build(tasks, ignored=Ignored(verb=True, colour=True, site=True))
    assert reapply(stored, after).frozen == 1
    assert [item.label for item in after.proposals if item.frozen] == ["mug handling"]

    scattered = {"left": {"a", "b"}, "right": {"c", "d"}}
    assert labels_for(scattered, [Confirmation(label="group", tasks=("a", "b", "c", "d"))]) == {}


def test_confirmation_cannot_cover_an_empty_task_set() -> None:
    from data_engine.catalog.clusters import Confirmation

    with pytest.raises(ValueError, match="at least one task"):
        Confirmation(label="empty", tasks=())
