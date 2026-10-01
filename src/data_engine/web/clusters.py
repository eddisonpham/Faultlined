"""Figures and panels for the Clusters page.

Pure functions from a `ProposalSet` to markup: a treemap of cluster sizes, a map of the
task strings with a boundary around each cluster, and the tables a reviewer needs to
confirm or reject a proposal. Same rules as `charts.py` (ADR 0024): server-rendered, no
JavaScript, no CDN, no dependency - and because a figure is a string, a test asserts its
geometry without a browser.

**The map is the honest part of this page.** Every task string is a point; a boundary is
drawn around the points of one cluster. A cluster whose boundary contains another
cluster's points is a merge the operator should reject, and a cluster reduced to a single
point is a fragmentation they should merge by hand. Both are visible in the drawing and
neither is visible in a count.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from data_engine.clustering import Proposal, ProposalSet, convex_hull, project, token_vector

__all__ = [
    "cluster_colour",
    "cluster_map",
    "empty_map",
    "health_table",
    "member_rows",
    "proposal_rows",
    "treemap",
]

FONT = 10.0

#: Treemap rows. Beyond this the labels are smaller than the eye resolves, so the page
#: shows the biggest clusters by size and lets the table carry the rest.
MAX_TREEMAP_ROWS = 6

#: Clusters per row before another row is added. Four is where the labels still fit
#: inside a cell without truncating every one of them.
PER_ROW_TARGET = 4

#: Categorical hues, spread around the wheel and checked for separation. Confirmed
#: clusters are drawn with the same hue and a heavier outline rather than a different
#: colour, so "confirmed" never looks like "a different cluster".
PALETTE: tuple[str, ...] = (
    "#2f6f9f",
    "#9f5b2f",
    "#4f8f4f",
    "#8f4f8f",
    "#9f9f2f",
    "#2f9f8f",
    "#8f5f5f",
    "#5f5f9f",
)


def cluster_colour(key: str) -> str:
    """A stable colour per cluster. Same key, same colour, on every render."""
    total = sum(ord(character) for character in key) or 1
    return PALETTE[total % len(PALETTE)]


def _svg(width: int, height: int, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f"<title>{escape(title)}</title>{body}</svg>"
    )


# --------------------------------------------------------------------- treemap


def treemap(proposals: Sequence[Proposal], *, width: int = 720, height: int = 320) -> str:
    """Cluster sizes as areas. Area is proportional to episodes, so a click means something."""
    if not proposals:
        return empty_map(width=width, height=height, message="no clusters yet")
    total = sum(proposal.size for proposal in proposals) or 1
    body: list[str] = []
    # Slice and dice: clusters are dealt into rows of roughly equal width, and each
    # cell's width is its share of *its row*. Every cluster gets exactly one rectangle -
    # the first version dealt `rows` clusters per row over `rows` rows and silently
    # dropped everything past rows*rows.
    rows = max(1, min(MAX_TREEMAP_ROWS, math.ceil(len(proposals) / PER_ROW_TARGET)))
    per_row = math.ceil(len(proposals) / rows)
    row_height = height / rows
    for row in range(rows):
        window = proposals[row * per_row : (row + 1) * per_row]
        share = sum(proposal.size for proposal in window) or 1
        x = 0.0
        for proposal in window:
            cell = width * (proposal.size / share)
            colour = cluster_colour(proposal.key)
            stroke = "#0b3d5c" if proposal.frozen else "#ffffff"
            rect_width = max(cell - 1.0, 0.5)
            body.append(
                f'<g><rect x="{x:.1f}" y="{row * row_height:.1f}" width="{rect_width:.1f}"'
                f' height="{row_height - 1.0:.1f}" fill="{colour}" fill-opacity="0.82"'
                f' stroke="{stroke}" stroke-width="{2.0 if proposal.frozen else 0.6}">'
                f"<title>{escape(proposal.label or proposal.core)} - {proposal.size} episodes,"
                f" {proposal.task_count} tasks</title></rect>"
            )
            if cell > 46:
                name = proposal.label or proposal.core
                limit = max(int(cell / 6.4), 4)
                body.append(
                    f'<text x="{x + 5:.1f}" y="{row * row_height + 16:.1f}" font-size="{FONT}"'
                    f' fill="#ffffff">{escape(name[:limit])}</text>'
                )
            body.append("</g>")
            x += cell
    caption = f"{len(proposals)} clusters over {total} episodes"
    body.append(
        f'<text x="0" y="{height - 4:.0f}" font-size="{FONT}" fill="#52606d">'
        f"{escape(caption)}</text>"
    )
    return _svg(width, height, "".join(body), f"cluster sizes: {caption}")


# ----------------------------------------------------------------------- the map


def empty_map(*, width: int = 720, height: int = 420, message: str) -> str:
    """The empty state. A chart that refuses to render is worse than one that is flat."""
    body = (
        f'<text x="16" y="{height / 2:.0f}" font-size="12" fill="#52606d">{escape(message)}</text>'
    )
    return _svg(width, height, body, message)


def _extent(points: Sequence[Sequence[float]], *, axis: int) -> float:
    """Half the span of one axis, so a point can sit at either end of the canvas."""
    values = [float(point[axis]) for point in points]
    return (max(values) - min(values)) / 2 if values else 0.0


def cluster_map(proposals: ProposalSet, *, width: int = 720, height: int = 420) -> str:
    """Every task string as a point, one boundary per cluster.

    Points are the *whole* task strings, not the extracted cores: members of one cluster
    share a core, so plotting cores would collapse each cluster to a single dot and the
    figure would show nothing about how tight the group is.
    """
    points: list[list[float]] = []
    owners: list[Proposal] = []
    for proposal in proposals.ordered():
        for member in proposal.members:
            points.append(token_vector(member.task))
            owners.append(proposal)
    if not points:
        return empty_map(width=width, height=height, message="no clusters yet")

    projected = project(points)
    pad = 28
    x_extent = _extent(projected.points, axis=0) or 1.0
    y_extent = _extent(projected.points, axis=1) or 1.0
    scale_x = (width - 2 * pad) / (2 * x_extent)
    scale_y = (height - 2 * pad) / (2 * y_extent)

    def place(index: int) -> tuple[float, float]:
        x, y = projected.points[index]
        return (width / 2 + x * scale_x, height / 2 - y * scale_y)

    placed = [place(index) for index in range(len(points))]
    by_cluster: dict[str, list[int]] = {}
    for index, proposal in enumerate(owners):
        by_cluster.setdefault(proposal.key, []).append(index)

    body: list[str] = []
    for key, indexes in by_cluster.items():
        proposal = next(item for item in owners if item.key == key)
        colour = cluster_colour(key)
        hull = convex_hull([placed[index] for index in indexes])
        if len(hull) >= 3:
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y in hull)
            body.append(
                f'<polygon points="{path}" fill="{colour}" fill-opacity="0.10"'
                f' stroke="{colour}" stroke-width="{2.0 if proposal.frozen else 0.9}"'
                f' stroke-dasharray="{"none" if proposal.frozen else "4 3"}">'
                f"<title>{escape(proposal.label or proposal.core)}</title></polygon>"
            )
        for index in indexes:
            x, y = placed[index]
            owner = owners[index]
            radius = 3.4 if owner.task_count > 1 else 2.6
            first_task = owner.members[0].task
            body.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}"'
                f' fill="{colour}" fill-opacity="0.85"><title>'
                f"{escape(owner.label or owner.core)}: {escape(first_task)}"
                f"</title></circle>"
            )

    note = (
        f"{len(points)} task strings, {len(by_cluster)} clusters, "
        f"{int(projected.explained * 100)}% of the spread shown"
        f" (each axis scaled to fit)"
    )
    if projected.dropped:
        note += f", {projected.dropped} beyond the page limit"
    body.append(
        f'<text x="0" y="{height - 6:.0f}" font-size="{FONT}" fill="#52606d">{escape(note)}</text>'
    )
    return _svg(width, height, "".join(body), f"task string map: {note}")


# ---------------------------------------------------------------------- the page


def health_table(proposals: ProposalSet) -> str:
    """The numbers first. A reviewer should not have to infer health from the picture."""
    health = proposals.health()
    cells = [
        ("tasks", health["tasks"]),
        ("episodes", health["episodes"]),
        ("clusters", health["clusters"]),
        ("singletons", health["singletons"]),
        ("merges", health["merges"]),
        ("confirmed", health["frozen"]),
        ("largest share", f"{float(health['dominance']) * 100:.1f}%"),
        ("verb matched", f"{float(health['verb_rate']) * 100:.0f}%"),
        ("single-token cores", f"{float(health['single_token_rate']) * 100:.0f}%"),
    ]
    tiles = "".join(
        f'<div class="de-stat"><span class="de-stat-value">{escape(str(value))}</span>'
        f'<span class="de-stat-label">{escape(name)}</span></div>'
        for name, value in cells
    )
    return f'<div class="de-stat-row">{tiles}</div>'


@dataclass(frozen=True, slots=True)
class Row:
    """One proposal as the table renders it."""

    key: str
    core: str
    label: str
    episodes: int
    tasks: int
    frozen: bool
    colour: str
    merged: int


def proposal_rows(proposals: ProposalSet) -> list[Row]:
    return [
        Row(
            key=proposal.key,
            core=proposal.core,
            label=proposal.label,
            episodes=proposal.size,
            tasks=proposal.task_count,
            frozen=proposal.frozen,
            colour=cluster_colour(proposal.key),
            merged=len(proposal.merged_cores),
        )
        for proposal in proposals.ordered()
    ]


def member_rows(proposal: Proposal) -> list[tuple[str, int]]:
    """Tasks behind a proposal, biggest first, so the name a reviewer picks is informed."""
    return [(member.task, member.episodes) for member in proposal.members]


def layout_note() -> str:
    """How the map was drawn, in the reader's terms rather than the maths."""
    return (
        "Points are task strings placed by shared words. Boundaries are hulls around one "
        "cluster's points, dashed while unconfirmed. A hull containing another cluster's "
        "points is a merge worth rejecting; a cluster of one point is a split worth "
        "joining. Axes carry no meaning."
    )
