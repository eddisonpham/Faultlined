"""White-box tests for the per-cluster detail view (ADR 0027)."""

from __future__ import annotations

from typing import Any

from data_engine.clustering import Ignored, build, proposal_key
from data_engine.web import cluster_page
from data_engine.web.pages import cluster_detail_fragment, cluster_detail_page


def _proposal(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key": proposal_key("mug"),
        "core": "mug",
        "label": "",
        "episodes": 20,
        "task_count": 3,
        "merged_cores": [],
    }
    base.update(overrides)
    return base


def _members() -> list[dict[str, Any]]:
    return [
        {
            "task": "pick up the red mug",
            "core": "mug",
            "episodes": 12,
            "verb": "pick up",
            "colours": ["red"],
        },
        {"task": "grab the mug", "core": "mug", "episodes": 5, "verb": "grab", "colours": []},
        {"task": "mug", "core": "mug", "episodes": 3, "verb": "", "colours": ["blue", "green"]},
    ]


def test_detail_tiles_show_identity_volume_and_open_status() -> None:
    html = cluster_page.detail_tiles(_proposal())
    assert "mug" in html
    assert '<span class="de-stat-value">20</span>' in html
    assert "proposal" in html
    assert "confirmed" not in html


def test_detail_tiles_name_the_confirmation_when_frozen() -> None:
    html = cluster_page.detail_tiles(_proposal(label="mug handling"))
    assert "confirmed: mug handling" in html


def test_detail_tiles_count_merged_cores() -> None:
    html = cluster_page.detail_tiles(_proposal(merged_cores=["mug", "cup"]))
    assert '<span class="de-stat-value">2</span>' in html


def test_detail_members_render_the_extraction_facts() -> None:
    html = cluster_page.detail_members(_members())
    assert "pick up the red mug" in html
    assert "pick up" in html
    assert "red" in html
    assert '<td class="num">3</td>' in html


def test_detail_members_show_a_placeholder_for_missing_verb_and_colours() -> None:
    html = cluster_page.detail_members(_members())
    assert f"<td>{chr(8212)}</td>" in html


def test_detail_members_escape_task_strings() -> None:
    hostile = [
        {
            "task": "<script>alert(1)</script>",
            "core": "mug",
            "episodes": 1,
            "verb": "",
            "colours": [],
        }
    ]
    html = cluster_page.detail_members(hostile)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_detail_members_say_so_when_empty() -> None:
    html = cluster_page.detail_members([])
    assert "no task strings recorded" in html


def test_detail_control_offers_confirm_for_an_open_proposal() -> None:
    html = cluster_page.detail_control(_proposal())
    assert f'action="/ui/clusters/{proposal_key("mug")}/confirm"' in html
    assert 'name="label"' in html
    assert 'value="mug"' in html


def test_detail_control_offers_release_for_a_confirmed_proposal() -> None:
    html = cluster_page.detail_control(_proposal(label="mug handling"))
    assert f'action="/ui/clusters/{proposal_key("mug")}/release"' in html
    assert "release confirmation" in html
    assert 'name="label"' not in html


def test_detail_control_is_preview_only_when_read_only() -> None:
    html = cluster_page.detail_control(_proposal(), read_only=True)
    assert "preview only" in html
    assert "<form" not in html


def test_detail_control_renders_an_escaped_error_banner() -> None:
    html = cluster_page.detail_control(_proposal(), error="<b>needs a label</b>")
    assert "<b>needs a label</b>" not in html
    assert "&lt;b&gt;needs a label&lt;/b&gt;" in html
    assert 'role="alert"' in html


def test_detail_body_links_back_and_lists_merged_cores() -> None:
    html = cluster_page.detail_body(_proposal(merged_cores=["mug", "cup"]), _members())
    assert 'href="/ui/clusters"' in html
    assert "merged cores: mug, cup" in html


def test_detail_body_omits_the_merged_note_for_a_single_core() -> None:
    html = cluster_page.detail_body(_proposal(), _members())
    assert "merged cores:" not in html


def test_detail_body_notes_truncation_instead_of_lying_about_completeness() -> None:
    html = cluster_page.detail_body(_proposal(task_count=250), _members()[:2])
    assert "showing the first 2 of 250" in html
    complete = cluster_page.detail_body(_proposal(task_count=3), _members())
    assert "showing the first" not in complete


def test_detail_body_carries_the_control_and_members() -> None:
    html = cluster_page.detail_body(_proposal(), _members())
    assert 'action="/ui/clusters/' in html
    assert "pick up the red mug" in html


def test_detail_page_is_a_full_document_on_the_clusters_nav() -> None:
    html = cluster_detail_page(_proposal(), _members(), "amber")
    assert html.startswith("<!doctype html>")
    assert 'data-theme="amber"' in html
    assert f"Cluster {proposal_key('mug')[:10]}" in html
    assert "Cluster detail" in html


def test_detail_page_title_survives_an_empty_key() -> None:
    html = cluster_detail_page(_proposal(key=""), _members(), "vt220")
    assert "Cluster ?" in html


def test_detail_fragment_is_a_body_not_a_document() -> None:
    html = cluster_detail_fragment(_proposal(), _members())
    assert "<!doctype html>" not in html
    assert 'href="/ui/clusters"' in html


def test_proposal_table_reports_the_true_hidden_count_into_the_detail_view() -> None:
    """Ten members must say "and 6 more", not a capped count."""
    colours = (
        "red",
        "blue",
        "green",
        "yellow",
        "white",
        "black",
        "orange",
        "pink",
        "purple",
        "brown",
    )
    many = {f"pick up the {colour} mug": index + 1 for index, colour in enumerate(colours)}
    proposals = build(many, ignored=Ignored(verb=True, colour=True))
    target = max(proposals.proposals, key=lambda item: item.size)
    assert target.task_count == 10
    html = cluster_page.proposal_table(proposals)
    assert "and 6 more" in html
    assert f'href="/ui/clusters/{target.key}"' in html
