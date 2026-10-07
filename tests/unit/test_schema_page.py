"""The schema page: layout, flow, and the states where the catalog is not there."""

from __future__ import annotations

import pytest

from data_engine.web.schema import (
    _layers,
    graph_svg,
    schema_fragment,
    table_detail,
)

pytestmark = pytest.mark.unit

MODEL = {
    "reachable": True,
    "error": "",
    "table_count": 4,
    "column_count": 6,
    "foreign_keys": [
        {
            "source": "episode_quality",
            "source_column": "episode_id",
            "target": "episodes",
            "target_column": "id",
        },
        {"source": "builds", "source_column": "job_id", "target": "jobs", "target_column": "id"},
    ],
    "tables": [
        {
            "name": "jobs",
            "row_count": 4,
            "referenced_by": ["builds"],
            "columns": [
                {
                    "name": "id",
                    "position": 1,
                    "data_type": "uuid",
                    "nullable": False,
                    "default": None,
                    "primary": True,
                },
                {
                    "name": "state",
                    "position": 2,
                    "data_type": "text",
                    "nullable": False,
                    "default": "'queued'",
                    "primary": False,
                },
            ],
        },
        {
            "name": "episodes",
            "row_count": 2,
            "referenced_by": ["episode_quality"],
            "columns": [
                {
                    "name": "id",
                    "position": 1,
                    "data_type": "uuid",
                    "nullable": False,
                    "default": None,
                    "primary": True,
                },
            ],
        },
        {
            "name": "builds",
            "row_count": 1,
            "referenced_by": [],
            "columns": [
                {
                    "name": "build_hash",
                    "position": 1,
                    "data_type": "text",
                    "nullable": False,
                    "default": None,
                    "primary": True,
                },
                {
                    "name": "job_id",
                    "position": 2,
                    "data_type": "uuid",
                    "nullable": False,
                    "default": None,
                    "primary": False,
                },
            ],
        },
        {
            "name": "episode_quality",
            "row_count": 2,
            "referenced_by": [],
            "columns": [
                {
                    "name": "episode_id",
                    "position": 1,
                    "data_type": "uuid",
                    "nullable": False,
                    "default": None,
                    "primary": True,
                },
                {
                    "name": "verdict",
                    "position": 2,
                    "data_type": "text",
                    "nullable": False,
                    "default": "'unknown'",
                    "primary": False,
                },
            ],
        },
    ],
}


@pytest.mark.unit
def test_a_table_that_references_nothing_is_the_first_layer() -> None:
    layers = _layers(MODEL["tables"], MODEL["foreign_keys"])
    assert layers["jobs"] == 0
    assert layers["episodes"] == 0


@pytest.mark.unit
def test_a_child_table_sits_below_its_parent() -> None:
    """The graph is a pipeline, so an edge must always point downward."""
    layers = _layers(MODEL["tables"], MODEL["foreign_keys"])
    assert layers["episode_quality"] > layers["episodes"]
    assert layers["builds"] > layers["jobs"]


@pytest.mark.unit
def test_layering_terminates_on_a_self_referencing_table() -> None:
    """A cycle must not hang the page; the schema has none, a future one might."""
    tables = [{"name": "a"}, {"name": "b"}]
    keys = [
        {"source": "a", "target": "b"},
        {"source": "b", "target": "a"},
        {"source": "a", "target": "a"},
    ]
    assert set(_layers(tables, keys)) == {"a", "b"}


@pytest.mark.unit
def test_the_graph_draws_one_node_per_table_and_one_edge_per_foreign_key() -> None:
    svg = graph_svg(MODEL)
    assert svg.count("de-node-link") == 4
    assert svg.count("<path") == 2
    assert svg.count("</svg>") == 1


@pytest.mark.unit
def test_every_node_is_a_link_to_that_tables_page() -> None:
    for name in ("jobs", "episodes", "episode_quality", "builds"):
        assert f'href="/ui/schema/{name}"' in graph_svg(MODEL)


@pytest.mark.unit
def test_a_node_shows_its_live_row_count() -> None:
    assert ">4 rows<" in graph_svg(MODEL)


@pytest.mark.unit
def test_the_graph_is_labelled_for_a_screen_reader() -> None:
    svg = graph_svg(MODEL)
    assert "catalog entity graph" in svg
    assert 'role="img"' in svg


@pytest.mark.unit
def test_a_foreign_key_to_a_table_outside_the_model_is_skipped_not_drawn() -> None:
    """A dangling edge would point at blank space and read as a rendering bug."""
    model = {
        **MODEL,
        "foreign_keys": MODEL["foreign_keys"]
        + [{"source": "jobs", "source_column": "x", "target": "ghost", "target_column": "id"}],
    }
    svg = graph_svg(model)
    assert svg.count("<path") == 2
    assert "ghost" not in svg


@pytest.mark.unit
def test_a_graph_with_no_tables_renders_nothing_rather_than_an_empty_frame() -> None:
    assert graph_svg({"tables": [], "foreign_keys": []}) == ""


@pytest.mark.unit
def test_a_flow_step_naming_a_missing_table_is_dropped_and_reported() -> None:
    """Drawing a step that 500s when clicked is worse than admitting it is behind."""
    body = schema_fragment(MODEL)
    assert "How data moves" in body
    assert "does not" in body and "have" in body


@pytest.mark.unit
def test_the_flow_names_the_real_tables_in_order() -> None:
    body = schema_fragment(MODEL)
    ingest = body[body.find(">ingest<") : body.find(">ingest<") + 400]
    assert ingest.index("jobs") < ingest.index("episodes")
    assert ingest.index("episodes") < ingest.index("episode_quality")


@pytest.mark.unit
def test_focusing_a_table_shows_its_columns_types_and_keys() -> None:
    detail = table_detail(MODEL, "episode_quality")
    assert "episode_id" in detail
    assert "uuid" in detail
    assert "key" in detail
    assert "2 rows" in detail
    assert "referenced by: none" in detail


@pytest.mark.unit
def test_focusing_a_table_that_does_not_exist_says_so() -> None:
    assert "no table named ghost" in table_detail(MODEL, "ghost")


@pytest.mark.unit
def test_the_page_puts_the_focused_table_first() -> None:
    body = schema_fragment(MODEL, "jobs")
    assert body.index(">jobs</h2>") < body.index("Entity graph")


@pytest.mark.unit
def test_an_unreachable_catalog_renders_a_state_rather_than_a_500() -> None:
    """A schema view that errors is worse than no schema view."""
    body = schema_fragment(
        {"reachable": False, "error": "OperationalError: no catalog", "tables": []}
    )
    assert "catalog unreachable" in body
    assert "no catalog" in body
    assert "<svg" not in body


@pytest.mark.unit
def test_the_page_reports_the_totals_it_read() -> None:
    body = schema_fragment(MODEL)
    assert ">4<" in body
    assert ">2<" in body
