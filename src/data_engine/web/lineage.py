"""Lineage as a graph (ADR 0007, drawn in ADR 0024).

The product's thesis is that a training set can be traced back to the exact
bytes and the exact code that produced it. Until now that answer existed only as
JSON: `/api/v1/episodes/{id}/builds` and `/api/v1/builds/{hash}` return it
correctly, and a human had to read it. A DAG is what lineage *is* - episodes
flow into a build, the build's policy and commit fix what came out - so drawing
it as a graph is not decoration, it is the same answer in the shape the question
was asked in.

Three columns, left to right, because the data moves one way:

    episodes  ->  build  ->  artifacts

Every node is a link. The graph is the navigation, not a picture next to a
table: clicking a node is the same action as reading its row, and there is no
second representation that can disagree with the first.
"""

from __future__ import annotations

from html import escape
from typing import Any

__all__ = ["builds_fragment", "builds_page", "lineage_page", "lineage_svg"]

_COL_W = 210.0
_COL_H = 30.0
_ROW_GAP = 9.0
_PAD_Y = 14.0


def _short(value: Any, keep: int = 10) -> str:
    text = str(value or "")
    return text[:keep] if len(text) > keep else text


def lineage_svg(build: dict[str, Any]) -> str:
    """Episodes, the build, and the artifacts, as one left-to-right graph.

    A build with more episodes than fit is truncated in the drawing and says so
    in the caption: a hundred stacked nodes is not a graph, it is a column of
    text pretending to be one. The full membership is in the table below it.
    """
    episodes = build.get("episodes") or []
    manifest = build.get("manifest") or {}
    members = manifest.get("episodes") if isinstance(manifest, dict) else None
    if members is None:
        members = episodes
    members = list(members or [])

    shown = members[:12]
    hidden = len(members) - len(shown)
    rows = max(len(shown), 1)
    height = _PAD_Y * 2 + rows * (_COL_H + _ROW_GAP)
    width = _COL_W * 3 + _PAD_Y * 2
    centre = _PAD_Y + (rows * (_COL_H + _ROW_GAP) - _ROW_GAP) / 2

    def y_at(index: int) -> float:
        return _PAD_Y + index * (_COL_H + _ROW_GAP)

    def box(x: float, y: float, label: str, sub: str, href: str) -> str:
        return (
            f'<a href="{escape(href)}" class="de-node-link">'
            f'<g class="de-node">'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{_COL_W:.1f}" '
            f'height="{_COL_H:.1f}" rx="2"/>'
            f'<text class="de-node-name" x="{x + 8:.1f}" y="{y + 13:.1f}">'
            f"{escape(label)}</text>"
            f'<text class="de-node-count" x="{x + 8:.1f}" y="{y + 25:.1f}">'
            f"{escape(sub)}</text></g></a>"
        )

    edges: list[str] = []
    nodes: list[str] = []

    build_x = _PAD_Y + _COL_W
    nodes.append(
        box(
            build_x,
            centre - _COL_H / 2,
            escape(str(build.get("name") or "build")),
            f"{_short(build.get('hash'), 12)} &#183; {len(members)} episodes",
            f"/ui/builds/{escape(str(build.get('hash')))}",
        )
    )

    for index, member in enumerate(shown):
        y = y_at(index)
        episode_id = str(member.get("episode_id") or "")
        nodes.append(
            box(
                _PAD_Y,
                y,
                f"{escape(str(member.get('format') or 'episode'))} {_short(episode_id, 8)}",
                f"source {_short(member.get('source_hash'), 10)}",
                f"/ui/episodes/{escape(episode_id)}",
            )
        )
        source_x = _PAD_Y + _COL_W
        target_x = build_x
        mid = (source_x + target_x) / 2
        edges.append(
            f'<path class="de-edge" d="M{source_x:.1f},{y + _COL_H / 2:.1f} '
            f"C{mid:.1f},{y + _COL_H / 2:.1f} {mid:.1f},{centre:.1f} "
            f'{target_x:.1f},{centre:.1f}">'
            f"<title>{escape(str(member.get('format') or ''))} "
            f"{_short(episode_id, 8)} &#8594; build</title></path>"
        )
        nodes.append(
            box(
                build_x + _COL_W,
                y,
                f"artifact {_short(member.get('artifact_hash'), 8)}",
                f"from {escape(str(member.get('format') or 'episode'))}",
                f"/ui/artifacts?hash={escape(str(member.get('artifact_hash') or ''))}",
            )
        )
        far = build_x + _COL_W * 2
        edges.append(
            f'<path class="de-edge" d="M{build_x + _COL_W:.1f},{centre:.1f} '
            f"C{far - 20:.1f},{centre:.1f} {far - 20:.1f},{y + _COL_H / 2:.1f} "
            f'{far:.1f},{y + _COL_H / 2:.1f}">'
            f"<title>build &#8594; artifact {_short(member.get('artifact_hash'), 10)}"
            f"</title></path>"
        )

    described = (
        f"lineage of build {build.get('name') or ''!s}: "
        f"{len(members)} episodes, "
        f"policy {_short(build.get('profile_hash'), 10)}, "
        f"commit {_short(build.get('code_commit'), 12)}"
    )
    note = (
        f'<p class="de-sub">showing {len(shown)} of {len(members)} episodes'
        f" &#183; the table below lists all of them</p>"
        if hidden > 0
        else ""
    )
    return (
        f'<svg class="de-plot de-graph" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'role="img" aria-label="{escape(described)}" '
        f'preserveAspectRatio="xMinYMin meet">'
        + f'<text class="de-tick" x="{_PAD_Y}" y="{_PAD_Y - 4}">episodes</text>'
        + f'<text class="de-tick" x="{build_x}" y="{_PAD_Y - 4}">build</text>'
        + f'<text class="de-tick" x="{build_x + _COL_W}" y="{_PAD_Y - 4}">'
        "artifacts</text>" + "".join(edges) + "".join(nodes) + "</svg>" + note
    )


def _members_table(build: dict[str, Any]) -> str:
    manifest = build.get("manifest") or {}
    members = list((manifest.get("episodes") if isinstance(manifest, dict) else None) or [])
    if not members:
        return '<p class="de-empty">// this build has no manifest</p>'
    rows = "".join(
        "<tr>"
        f'<td><a href="/ui/episodes/{escape(str(m.get("episode_id")))}">'
        f"<code>{escape(str(m.get('episode_id') or ''))[:12]}</code></a></td>"
        f"<td>{escape(str(m.get('format') or '-'))}</td>"
        f"<td><code>{escape(str(m.get('source_hash') or '-')[:16])}</code></td>"
        f"<td><code>{escape(str(m.get('artifact_hash') or '-')[:16])}</code></td>"
        "</tr>"
        for m in members
    )
    return (
        '<div class="de-table-wrap" tabindex="0" role="region" '
        'aria-label="Episodes in this build"><table class="de-table">'
        '<caption class="de-sr">Every episode in this build, with its source '
        "and artifact addresses</caption><thead><tr><th>Episode</th><th>Format</th>"
        "<th>Source</th><th>Artifact</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _index_body(model: dict[str, Any]) -> str:
    items = model.get("items") or []
    if not items:
        return (
            '<p class="de-empty">// no builds yet</p>'
            '<p class="de-sub">a build is created by a build job, and its address'
            " is the content of everything it contains</p>"
        )
    rows = "".join(
        "<tr>"
        f'<td><a href="/ui/builds/{escape(str(b.get("hash")))}">'
        f"<code>{escape(str(b.get('name') or '-'))}</code></a></td>"
        f"<td><code>{escape(str(b.get('hash') or '')[:16])}</code></td>"
        f'<td class="num">{int(b.get("episode_count") or 0)}</td>'
        f"<td><code>{escape(str(b.get('profile_hash') or '-')[:12])}</code></td>"
        f"<td><code>{escape(str(b.get('code_commit') or '-')[:12])}</code></td>"
        f'<td><a href="/ui/jobs/{escape(str(b.get("job_id")))}">job</a></td>'
        f"<td>{escape(str(b.get('created_at') or '-')[:19])}</td>"
        "</tr>"
        for b in items
    )
    return (
        '<div class="de-table-wrap" tabindex="0" role="region" '
        'aria-label="Content-addressed builds"><table class="de-table">'
        '<caption class="de-sr">Builds, newest first</caption><thead><tr>'
        '<th>Name</th><th>Address</th><th class="num">Episodes</th>'
        "<th>Policy</th><th>Commit</th><th>Job</th><th>Created</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _detail_body(build: dict[str, Any]) -> str:
    if not build:
        return '<p class="de-empty">// no such build</p>'
    from data_engine.web.pages import _copyable, _readouts, _section, _when

    readouts = _readouts(
        [
            ("episodes", str(int(build.get("episode_count") or 0)), ""),
            (
                "policy",
                _copyable(
                    str(build.get("profile_hash") or ""), str(build.get("profile_hash") or "-")[:12]
                ),
                "",
            ),
            (
                "commit",
                _copyable(
                    str(build.get("code_commit") or ""), str(build.get("code_commit") or "-")[:12]
                ),
                "",
            ),
            ("job", f'<a href="/ui/jobs/{escape(str(build.get("job_id")))}">open</a>', ""),
            ("created", _when(build.get("created_at")), ""),
        ]
    )
    return (
        readouts
        + _section(
            "Lineage",
            lineage_svg(build) + '<p class="de-sub">every node is a link &#183; the address is the'
            " content, so two runs over the same episodes and the same policy"
            " produce the same build</p>",
            "episodes to build to artifacts",
        )
        + _section("Members", _members_table(build), str(int(build.get("episode_count") or 0)))
    )


def builds_fragment(model: dict[str, Any]) -> str:
    return _index_body(model)


def builds_page(
    model: dict[str, Any],
    theme: str,
    *,
    error: str = "",
    values: dict[str, str] | None = None,
) -> str:
    """Builds, newest first, and the form that makes one (FR-006)."""
    from data_engine.web.pages import _page, build_form

    return _page(
        "Builds", "/ui/builds", _index_body(model) + build_form(error, values, theme), theme
    )


def lineage_page(
    build: dict[str, Any],
    theme: str,
    *,
    error: str = "",
    values: dict[str, str] | None = None,
) -> str:
    from data_engine.web.pages import _page, export_form

    body = _detail_body(build)
    if build:
        # The export form is only offered for a build that exists: an export is a
        # projection of an immutable manifest, and this page is the manifest.
        body += export_form(str(build.get("hash") or ""), error, values, theme)
    return _page(f"Build {str(build.get('name') or '')[:24]}", "/ui/builds", body, theme)
