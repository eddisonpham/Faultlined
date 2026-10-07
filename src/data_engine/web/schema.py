"""Schema and data-flow page (ADR 0024)."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import Any

__all__ = ["graph_svg", "schema_fragment", "schema_page", "table_detail"]

FLOW: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ingest", ("jobs", "episodes", "episode_quality", "artifacts")),
    ("validate", ("jobs", "validation_profiles", "validation_results", "episodes")),
    ("build", ("jobs", "builds", "build_episodes", "artifacts", "lineage_edges")),
    ("curate", ("episodes", "slices", "slice_memberships", "lineage_edges")),
    ("monitor", ("jobs", "monitor_baselines", "monitor_incidents", "incidents")),
)

_NODE_W = 168.0
_NODE_H = 34.0
_GAP_X = 26.0
_GAP_Y = 58.0
_PAD = 16.0


def _layers(tables: Sequence[dict[str, Any]], keys: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Assign each table a layer: 0 references nothing, n references layer < n."""
    references: dict[str, set[str]] = {str(table["name"]): set() for table in tables}
    for key in keys:
        source = str(key["source"])
        target = str(key["target"])
        if source in references and target in references and source != target:
            references[source].add(target)

    layer: dict[str, int] = {}

    def depth(name: str, seen: frozenset[str]) -> int:
        if name in layer:
            return layer[name]
        if name in seen:
            return 0
        parents = references.get(name, set())
        value = 0 if not parents else 1 + max(depth(parent, seen | {name}) for parent in parents)
        layer[name] = value
        return value

    for table in tables:
        depth(str(table["name"]), frozenset())
    return layer


def _positions(
    tables: Sequence[dict[str, Any]], layers: dict[str, int]
) -> tuple[dict[str, tuple[float, float]], float, float]:
    """Left-to-right within a layer, centred vertically, one row per layer."""
    by_layer: dict[int, list[str]] = {}
    for table in tables:
        by_layer.setdefault(layers.get(str(table["name"]), 0), []).append(str(table["name"]))
    tallest = max((len(names) for names in by_layer.values()), default=1)
    height = _PAD * 2 + tallest * _NODE_H + (tallest - 1) * _GAP_Y
    width = _PAD * 2 + (max(by_layer) + 1) * (_NODE_W + _GAP_X)

    placed: dict[str, tuple[float, float]] = {}
    for index, level in enumerate(sorted(by_layer)):
        names = sorted(by_layer[level])
        column_height = len(names) * _NODE_H + (len(names) - 1) * _GAP_Y
        start = (height - column_height) / 2
        for row, name in enumerate(names):
            placed[name] = (
                _PAD + index * (_NODE_W + _GAP_X),
                start + row * (_NODE_H + _GAP_Y),
            )
    return placed, width, height


def graph_svg(model: dict[str, Any]) -> str:
    """The entity graph, drawn from the live foreign keys."""
    tables = model.get("tables") or []
    keys = model.get("foreign_keys") or []
    if not tables:
        return ""

    layers = _layers(tables, keys)
    placed, width, height = _positions(tables, layers)
    counts = {str(table["name"]): int(table.get("row_count") or 0) for table in tables}

    edges = []
    for key in keys:
        source = placed.get(str(key["source"]))
        target = placed.get(str(key["target"]))
        if source is None or target is None:
            continue
        x1 = source[0] + _NODE_W
        y1 = source[1] + _NODE_H / 2
        x2 = target[0]
        y2 = target[1] + _NODE_H / 2
        mid = (x1 + x2) / 2
        edges.append(
            f'<path class="de-edge" d="M{x1:.1f},{y1:.1f} C{mid:.1f},{y1:.1f} '
            f'{mid:.1f},{y2:.1f} {x2:.1f},{y2:.1f}">'
            f"<title>{escape(str(key['source']))}.{escape(str(key['source_column']))}"
            f" &#8594; {escape(str(key['target']))}.{escape(str(key['target_column']))}"
            f"</title></path>"
        )

    nodes = []
    for name, (x, y) in sorted(placed.items()):
        rows = counts.get(name, 0)
        label = escape(name)
        nodes.append(
            f'<a href="/ui/schema/{escape(name)}" class="de-node-link">'
            f'<g class="de-node">'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{_NODE_W:.1f}" height="{_NODE_H:.1f}" '
            f'rx="2"/>'
            f'<text class="de-node-name" x="{x + 8:.1f}" y="{y + 14:.1f}">{label}</text>'
            f'<text class="de-node-count" x="{x + 8:.1f}" y="{y + 27:.1f}">'
            f"{rows} rows</text>"
            f"</g></a>"
        )

    described = (
        f"catalog entity graph: {len(tables)} tables, {len(keys)} foreign keys, "
        f"{max(layers.values(), default=0) + 1} layers"
    )
    return (
        f'<svg class="de-plot de-graph" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'role="img" aria-label="{escape(described)}" '
        f'preserveAspectRatio="xMinYMin meet">' + "".join(edges) + "".join(nodes) + "</svg>"
    )


def flow_svg(model: dict[str, Any]) -> str:
    """The path data takes, as labelled chains over the tables that exist."""
    present = {str(table["name"]) for table in model.get("tables") or []}
    chains = [(name, [step for step in steps if step in present]) for name, steps in FLOW]
    chains = [(name, steps) for name, steps in chains if len(steps) > 1]

    rows = []
    missing = 0
    for name, steps in chains:
        if len(steps) < len(dict(FLOW)[name]):
            missing += 1
        arrows = " &#8594; ".join(f'<code class="de-chip">{escape(step)}</code>' for step in steps)
        rows.append(f"<tr><td><code>{escape(name)}</code></td><td>{arrows}</td></tr>")
    if not rows:
        return ""
    note = (
        f'<p class="de-sub">{missing} path(s) name a table this catalog does not'
        " have &#183; the page is showing the code, not the database</p>"
        if missing
        else ""
    )
    return (
        '<div class="de-table-wrap" tabindex="0" role="region" '
        'aria-label="Data flow through the catalog"><table class="de-table">'
        '<caption class="de-sr">The path data takes through the catalog</caption>'
        f"<thead><tr><th>Path</th><th>Tables, in order</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>" + note
    )


def table_detail(model: dict[str, Any], name: str) -> str:
    """Columns, types, keys and row count for one table."""
    for table in model.get("tables") or []:
        if str(table["name"]) != name:
            continue
        columns = table.get("columns") or []
        rows = "".join(
            "<tr>"
            f"<td><code>{escape(str(column['name']))}</code></td>"
            f"<td>{escape(str(column['data_type']))}</td>"
            f"<td>{'yes' if column['nullable'] else 'no'}</td>"
            f"<td>{'key' if column['primary'] else '-'}</td>"
            f'<td><code class="de-dim">{escape(str(column["default"] or "-"))[:60]}</code></td>'
            "</tr>"
            for column in columns
        )
        targets = ", ".join(str(t) for t in table.get("referenced_by") or []) or "none"
        return (
            f'<p class="de-sub">{int(table.get("row_count") or 0)} rows '
            f"&#183; {len(columns)} columns &#183; referenced by: "
            f"{escape(targets)}</p>"
            '<div class="de-table-wrap" tabindex="0" role="region" '
            f'aria-label="Columns of {escape(name)}"><table class="de-table">'
            f'<caption class="de-sr">Columns of {escape(name)}</caption>'
            "<thead><tr><th>Column</th><th>Type</th><th>Nullable</th><th>Key</th>"
            "<th>Default</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
        )
    return f'<p class="de-empty">// no table named {escape(name)}</p>'


def _schema_body(model: dict[str, Any], focus: str = "") -> str:
    if not model.get("reachable", False):
        return (
            '<p class="de-empty">// catalog unreachable</p>'
            f'<p class="de-sub">{escape(str(model.get("error") or ""))}</p>'
            '<p class="de-sub">this page reads the running database, so it has'
            " nothing to show until the catalog answers</p>"
        )

    tables = model.get("tables") or []
    readouts = (
        '<dl class="de-readouts">'
        f'<div class="de-readout block-item"><dt>tables</dt>'
        f'<dd class="current-value">{int(model.get("table_count") or 0)}</dd></div>'
        f'<div class="de-readout block-item"><dt>columns</dt>'
        f'<dd class="current-value">{int(model.get("column_count") or 0)}</dd></div>'
        f'<div class="de-readout block-item"><dt>foreign keys</dt>'
        f'<dd class="current-value">{len(model.get("foreign_keys") or [])}</dd></div>'
        f'<div class="de-readout block-item"><dt>rows</dt>'
        f'<dd class="current-value">'
        f"{sum(int(t.get('row_count') or 0) for t in tables)}</dd></div>"
        "</dl>"
    )

    index_rows = "".join(
        "<tr>"
        f'<td><a href="/ui/schema/{escape(str(table["name"]))}">'
        f"<code>{escape(str(table['name']))}</code></a></td>"
        f'<td class="num">{len(table.get("columns") or [])}</td>'
        f'<td class="num">{int(table.get("row_count") or 0)}</td>'
        f"<td>{escape(', '.join(str(t) for t in table.get('referenced_by') or []) or '-')}</td>"
        "</tr>"
        for table in tables
    )

    sections = [
        '<section class="de-section fine-use-component">'
        '<h2>Entity graph<span class="de-bezel-label">from the real '
        "constraints</span></h2>"
        + (graph_svg(model) or '<p class="de-empty">// no tables</p>')
        + '<p class="de-sub">click a table for its columns &#183; edges are the'
        " database's own foreign keys, not a drawing</p></section>",
        '<section class="de-section fine-use-component">'
        "<h2>Tables</h2>"
        '<div class="de-table-wrap" tabindex="0" role="region" aria-label="Catalog tables">'
        '<table class="de-table"><caption class="de-sr">Catalog tables, live row '
        'counts</caption><thead><tr><th>Table</th><th class="num">Columns</th>'
        '<th class="num">Rows</th><th>Referenced by</th></tr></thead>'
        f"<tbody>{index_rows}</tbody></table></div></section>",
    ]
    flow = flow_svg(model)
    if flow:
        sections.append(
            '<section class="de-section fine-use-component">'
            '<h2>How data moves<span class="de-bezel-label">ingest to '
            "monitoring</span></h2>" + flow + "</section>"
        )
    if focus:
        sections.insert(
            0,
            '<section class="de-section fine-use-component">'
            f"<h2>{escape(focus)}</h2>" + table_detail(model, focus) + "</section>",
        )
    return readouts + "".join(sections)


def schema_page(model: dict[str, Any], theme: str, focus: str = "") -> str:
    from data_engine.web.pages import _page

    return _page("Schema", "/ui/schema", _schema_body(model, focus), theme)


def schema_fragment(model: dict[str, Any], focus: str = "") -> str:
    return _schema_body(model, focus)
