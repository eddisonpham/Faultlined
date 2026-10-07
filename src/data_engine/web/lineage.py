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


def _redundancy_section(build: dict[str, Any]) -> str:
    """Which members are the same behaviour recorded twice (ADR 0032).

    Read-only, and it says so: a report that proposed collapses without stating that nothing
    has been removed would read as a decision already taken. The reason travels with every
    row, because the whole justification for a deterministic fingerprint over a model's
    embedding space is that an operator can disagree with it by reading it.
    """
    from data_engine.web.pages import _meter, _readouts, _section

    report = build.get("redundancy")
    if not isinstance(report, dict):
        return ""
    scored = int(report.get("episode_count") or 0)
    if scored < 2:
        return _section(
            "Redundancy",
            '<p class="de-sub">// fewer than two scored episodes: nothing to compare</p>',
            f"{scored} scored",
        )
    distinct = int(report.get("distinct_count") or 0)
    redundant = int(report.get("redundant_count") or 0)
    ratio = float(report.get("reduction_ratio") or 0.0)
    threshold = float(report.get("threshold") or 0.0)
    incomparable = [str(item) for item in (report.get("incomparable") or [])]
    groups = [g for g in (report.get("groups") or []) if isinstance(g, dict)]

    rows = "".join(
        "<tr>"
        f'<td><a href="/ui/episodes/{escape(str(group.get("representative")))}">'
        f"{escape(str(group.get('label') or group.get('representative') or ''))[:48]}</a></td>"
        f'<td><a href="/ui/episodes/{escape(str(dup.get("episode_id")))}">'
        f"{escape(str(dup.get('label') or dup.get('episode_id') or ''))[:48]}</a></td>"
        f'<td class="num">{float(dup.get("distance") or 0.0):.3f}</td>'
        f"<td>{escape(str(dup.get('reason') or ''))}</td>"
        "</tr>"
        for group in groups
        for dup in (group.get("duplicates") or [])
        if isinstance(dup, dict)
    )
    if rows:
        table = (
            '<div class="de-table-wrap" tabindex="0" role="region" '
            'aria-label="Near-duplicate episodes in this build"><table class="de-table">'
            '<caption class="de-sr">Episodes a redundancy report would collapse onto a kept'
            " representative, with the distance and the component that dominated it"
            "</caption><thead><tr><th>Keep</th><th>Near-duplicate</th>"
            '<th class="num">Distance</th><th>Dominant signal</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>"
        )
    else:
        table = '<p class="de-sub">// every scored episode is distinct</p>'

    notes = [
        f'<p class="de-sub">threshold {threshold:.2f} on a weighted mean of motion shape,'
        " dimension dynamics and temporal fractions &#183; a proposal, read-only &#8212;"
        " nothing here is removed from the build</p>"
    ]
    if incomparable:
        notes.append(
            f'<p class="de-sub">{len(incomparable)} scored episode(s) carry no motion to'
            " compare on (no clock, no judged dimension) and are counted as distinct</p>"
        )
    if report.get("truncated"):
        notes.append(
            '<p class="de-sub">this build is larger than one report scores:'
            f" the first {scored} members in build order were compared, the rest were not</p>"
        )

    return _section(
        "Redundancy",
        _readouts(
            [
                ("scored", str(scored), ""),
                ("distinct", str(distinct), ""),
                ("near-duplicate", str(redundant), ""),
                ("would shrink", f"{ratio * 100:.0f}%", ""),
            ]
        )
        + f'<p class="de-sub">{_meter(ratio)} of this build repeats behaviour it already has</p>'
        + table
        + "".join(notes),
        f"{distinct} of {scored} distinct",
    )


def _coverage_section(build: dict[str, Any]) -> str:
    """What this build holds on each axis, and what the catalog holds instead (ADR 0033).

    The gap column is the point of the section. A distribution alone describes the build; the set
    difference against the catalog is the only part an operator can act on, and for tasks it is
    read from the vocabulary, so a label the operator named and never recorded appears here as
    missing rather than not existing.
    """
    from data_engine.web.pages import _meter, _readouts, _section

    report = build.get("coverage")
    if not isinstance(report, dict):
        return ""
    axes = [axis for axis in (report.get("axes") or []) if isinstance(axis, dict)]
    if not axes:
        return ""
    episode_count = int(report.get("episode_count") or 0)
    catalog_size = int(report.get("catalog_size") or 0)
    ratio = float(report.get("coverage_ratio") or 0.0)
    vocabulary_total = int(report.get("vocabulary_total") or 0)
    vocabulary_missing = int(report.get("vocabulary_missing") or 0)

    def holds(axis: dict[str, Any]) -> str:
        values = [item for item in (axis.get("values") or []) if isinstance(item, dict)]
        if not values:
            return '<span class="de-sub">none</span>'
        return " &#183; ".join(
            f"{escape(str(item.get('value') or ''))} "
            f'<span class="num">{int(item.get("count") or 0)}</span>'
            for item in values
        )

    def lacks(axis: dict[str, Any]) -> str:
        gaps = [str(gap) for gap in (axis.get("gaps") or [])]
        missing = int(axis.get("missing") or 0)
        if not gaps:
            return '<span class="de-sub">nothing on this axis</span>'
        shown = ", ".join(escape(gap) for gap in gaps)
        if axis.get("truncated") and missing > len(gaps):
            shown += f' <span class="de-sub">and {missing - len(gaps)} more</span>'
        return shown

    rows = "".join(
        "<tr>"
        f"<td>{escape(str(axis.get('label') or axis.get('axis') or ''))}</td>"
        f'<td class="num">{int(axis.get("present") or 0)}</td>'
        f"<td>{holds(axis)}</td>"
        f"<td>{lacks(axis)}</td>"
        "</tr>"
        for axis in axes
    )
    table = (
        '<div class="de-table-wrap" tabindex="0" role="region" '
        'aria-label="Coverage of this build"><table class="de-table">'
        '<caption class="de-sr">Each axis the catalog records, how many distinct values this'
        " build holds, which values those are, and which values the catalog holds that this build"
        ' does not</caption><thead><tr><th>Axis</th><th class="num">Values</th>'
        "<th>This build holds</th><th>Missing from this build</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )

    return _section(
        "Coverage",
        _readouts(
            [
                ("episodes", str(episode_count), ""),
                ("catalog", str(catalog_size), ""),
                ("of catalog", f"{ratio * 100:.0f}%", ""),
                (
                    "task gaps",
                    f"{vocabulary_missing} / {vocabulary_total}",
                    "",
                ),
            ]
        )
        + f'<p class="de-sub">{_meter(ratio)} of this catalog\u2019s episodes, on every axis'
        " &#183; read-only: nothing here removes an episode or changes the build\u2019s address</p>"
        + table
        + '<p class="de-sub">per axis at most 20 values and 20 gaps are listed, with the true'
        " totals carried beside them, so the payload is bounded by the axes rather than by the"
        " episode count</p>",
        f"{len(axes)} axes &#183; {vocabulary_missing} of {vocabulary_total} tasks absent",
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
        + _redundancy_section(build)
        + _coverage_section(build)
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
