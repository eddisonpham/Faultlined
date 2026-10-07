"""The lineage graph (ADR 0007, drawn in ADR 0024)."""

from __future__ import annotations

import pytest

from data_engine.web.lineage import (
    _index_body,
    lineage_page,
    lineage_svg,
)

pytestmark = pytest.mark.unit


def _build(count: int = 3, *, manifest: bool = True) -> dict:
    members = [
        {
            "episode_id": f"ep-{i}",
            "format": "lerobot-v3" if i % 2 else "mcap",
            "source_hash": f"{i:064d}",
            "artifact_hash": f"{i + 100:064d}",
        }
        for i in range(count)
    ]
    return {
        "hash": "b" * 64,
        "name": "so101-v1",
        "episode_count": count,
        "profile_hash": "p" * 64,
        "code_commit": "f5a9015ccb76",
        "job_id": "job-1",
        "created_at": "2026-09-30T04:00:00+00:00",
        "episodes": members,
        "manifest": {"episodes": members} if manifest else {},
    }


@pytest.mark.unit
def test_the_graph_draws_a_node_per_episode_plus_the_build_itself() -> None:
    svg = lineage_svg(_build(3))
    assert svg.count("de-node-link") == 1 + 3 * 2


@pytest.mark.unit
def test_every_episode_node_links_to_that_episode() -> None:
    svg = lineage_svg(_build(3))
    for i in range(3):
        assert f'href="/ui/episodes/ep-{i}"' in svg


@pytest.mark.unit
def test_the_build_node_links_to_the_build_page() -> None:
    assert f'href="/ui/builds/{"b" * 64}"' in lineage_svg(_build(2))


@pytest.mark.unit
def test_the_graph_is_labelled_with_what_it_proves() -> None:
    """The point of the page is reproducibility, so the label states it."""
    svg = lineage_svg(_build(2))
    assert "lineage of build so101-v1" in svg
    assert "2 episodes" in svg
    assert "commit f5a9015ccb7" in svg


@pytest.mark.unit
def test_a_long_build_is_truncated_in_the_drawing_and_says_so() -> None:
    """A hundred stacked nodes is a column of text pretending to be a graph."""
    svg = lineage_svg(_build(40))
    assert svg.count("de-node-link") == 1 + 12 * 2
    assert "showing 12 of 40" in svg


@pytest.mark.unit
def test_a_short_build_does_not_claim_to_be_truncated() -> None:
    assert "showing" not in lineage_svg(_build(3))


@pytest.mark.unit
def test_a_build_with_no_manifest_renders_without_pretending_it_has_members() -> None:
    svg = lineage_svg(_build(0))
    assert "<svg" in svg
    assert "0 episodes" in svg


@pytest.mark.unit
def test_the_detail_page_links_to_the_job_that_made_the_build() -> None:
    body = lineage_page(_build(2), "vt220")
    assert 'href="/ui/jobs/job-1"' in body


@pytest.mark.unit
def test_the_detail_page_cites_the_policy_and_the_commit() -> None:
    body = lineage_page(_build(2), "vt220")
    assert "p" * 12 in body
    assert "f5a9015ccb7" in body


@pytest.mark.unit
def test_the_members_table_lists_every_episode_the_graph_truncated() -> None:
    body = lineage_page(_build(40), "vt220")
    for i in range(40):
        assert f"/ui/episodes/ep-{i}" in body


@pytest.mark.unit
def test_a_build_that_does_not_exist_says_so_instead_of_drawing_an_empty_graph() -> None:
    body = lineage_page({}, "vt220")
    assert "no such build" in body
    assert 'class="de-graph"' not in body


@pytest.mark.unit
def test_the_index_says_what_would_create_a_build_when_there_are_none() -> None:
    body = _index_body({"items": []})
    assert "no builds yet" in body
    assert "build job" in body


@pytest.mark.unit
def test_the_index_links_each_build_to_its_page() -> None:
    body = _index_body({"items": [_build(2)]})
    assert f'href="/ui/builds/{"b" * 64}"' in body


def _report(**overrides: object) -> dict:
    return {
        "method": "behavioural-fingerprint-v1",
        "threshold": 0.1,
        "episode_count": 12,
        "compared_count": 12,
        "distinct_count": 4,
        "redundant_count": 8,
        "reduction_ratio": 8 / 12,
        "incomparable": [],
        "groups": [
            {
                "representative": "ep-0",
                "label": "pick the cube",
                "duplicates": [
                    {
                        "episode_id": "ep-1",
                        "label": "pick cube",
                        "distance": 0.03,
                        "reason": "shape",
                    }
                ],
            }
        ],
        "truncated": False,
        **overrides,
    }


@pytest.mark.unit
def test_a_build_without_a_report_renders_without_one() -> None:
    """The index and every caller that has no membership must not grow an empty panel."""
    assert "Redundancy" not in lineage_page(_build(2), "vt220")


@pytest.mark.unit
def test_the_redundancy_section_links_both_sides_of_a_duplicate_pair() -> None:
    body = lineage_page({**_build(2), "redundancy": _report()}, "vt220")
    assert 'href="/ui/episodes/ep-0"' in body
    assert 'href="/ui/episodes/ep-1"' in body
    assert "pick cube" in body


@pytest.mark.unit
def test_the_redundancy_section_states_the_measure_and_that_nothing_was_removed() -> None:
    """A report that proposed collapses without saying it is read-only would read as a decision
    already taken."""
    body = lineage_page({**_build(2), "redundancy": _report()}, "vt220")
    assert "threshold 0.10" in body
    assert "read-only" in body
    assert "4 of 12 distinct" in body


@pytest.mark.unit
def test_a_truncated_report_says_which_members_it_did_not_score() -> None:
    body = lineage_page({**_build(2), "redundancy": _report(truncated=True)}, "vt220")
    assert "the first 12 members in build order were compared" in body


@pytest.mark.unit
def test_episodes_that_could_not_be_compared_are_named_as_a_count() -> None:
    body = lineage_page(
        {**_build(2), "redundancy": _report(incomparable=["ep-7", "ep-8"])}, "vt220"
    )
    assert "2 scored episode(s) carry no motion to compare on" in body


@pytest.mark.unit
def test_a_build_with_one_episode_says_there_is_nothing_to_compare() -> None:
    body = lineage_page(
        {
            **_build(1),
            "redundancy": _report(episode_count=1, distinct_count=1, redundant_count=0, groups=[]),
        },
        "vt220",
    )
    assert "fewer than two scored episodes" in body
    assert "de-table" in body


@pytest.mark.unit
def test_a_fully_distinct_build_says_so_rather_than_showing_an_empty_table() -> None:
    body = lineage_page(
        {
            **_build(2),
            "redundancy": _report(
                distinct_count=12, redundant_count=0, groups=[], reduction_ratio=0.0
            ),
        },
        "vt220",
    )
    assert "every scored episode is distinct" in body
    assert "Near-duplicate episodes in this build" not in body


def _coverage(**overrides: object) -> dict:
    """A coverage report in the shape the JSON route serves."""

    def axis(name: str, label: str, values: list[dict], gaps: list[str], missing: int) -> dict:
        return {
            "axis": name,
            "label": label,
            "present": len(values),
            "values": values,
            "gaps": gaps,
            "missing": missing,
            "truncated": missing > len(gaps),
        }

    return {
        "method": "build-coverage-v1",
        "build_hash": "b" * 64,
        "episode_count": 2,
        "catalog_size": 5,
        "coverage_ratio": 0.4,
        "axes": [
            axis(
                "task",
                "Task",
                [{"value": "pick", "count": 2, "share": 1.0}],
                ["fold", "place the can"],
                2,
            ),
            axis("robot", "Embodiment", [{"value": "so101", "count": 2, "share": 1.0}], [], 0),
        ],
        "vocabulary_total": 3,
        "vocabulary_missing": 2,
        **overrides,
    }


@pytest.mark.unit
def test_a_build_without_a_coverage_report_renders_without_one() -> None:
    assert "Coverage" not in lineage_page(_build(2), "vt220")


@pytest.mark.unit
def test_the_coverage_section_names_the_gaps_beside_what_the_build_holds() -> None:
    """The set difference is the actionable half, so it is on the page, not only in the JSON."""
    body = lineage_page({**_build(2), "coverage": _coverage()}, "vt220")

    assert "Coverage" in body
    assert 'pick <span class="num">2</span>' in body
    assert "fold, place the can" in body
    assert "2 of 3 tasks absent" in body
    assert "read-only" in body, "nothing here removes an episode or changes the address"


@pytest.mark.unit
def test_a_capped_axis_is_drawn_with_the_true_total_beside_it() -> None:
    body = lineage_page({**_build(2), "coverage": _coverage() | {"axes": []}}, "vt220")
    assert "Coverage" not in body, "an empty axis list is not a coverage report"

    capped = _coverage()
    capped["axes"][0] |= {"gaps": ["fold"], "missing": 25, "truncated": True}
    body = lineage_page({**_build(2), "coverage": capped}, "vt220")

    assert "and 24 more" in body
    assert "at most 20 values and 20 gaps" in body


@pytest.mark.unit
def test_a_coverage_report_in_a_shape_the_page_cannot_read_is_skipped() -> None:
    """Better no panel than a panel of zeros that reads like a measured catalog of nothing."""
    for broken in ({"axes": ["not an axis"]}, "not a report", {"axes": None}):
        body = lineage_page({**_build(2), "coverage": broken}, "vt220")
        assert "Coverage" not in body
        assert "Members" in body, "the rest of the page is still drawn"
