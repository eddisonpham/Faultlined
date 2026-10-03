"""Server-rendered cluster summaries and a quantitative class-size view (ADR 0027)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from data_engine.clustering import Proposal, ProposalSet

__all__ = ["class_size_view", "cluster_colour", "health_table", "member_rows", "proposal_rows"]

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
    """A stable identity swatch in the proposal table."""
    total = sum(ord(character) for character in key) or 1
    return PALETTE[total % len(PALETTE)]


def class_size_view(proposals: Sequence[Proposal]) -> str:
    """Rank every proposal by episode volume; length is relative to the largest."""
    if not proposals:
        return '<p class="de-empty">// no classes to compare yet</p>'
    ordered = sorted(proposals, key=lambda item: (-item.size, item.core))
    total = sum(item.size for item in ordered)
    largest = max((item.size for item in ordered), default=0) or 1
    rows: list[str] = []
    for rank, proposal in enumerate(ordered, start=1):
        share = proposal.size / total if total else 0.0
        relative = proposal.size / largest * 100
        label = proposal.label or proposal.core
        status = "human-confirmed" if proposal.frozen else "proposal"
        rows.append(
            f'<li class="de-class-size-row" aria-label="{rank}. {escape(label)}, '
            f"{proposal.size} episodes, {proposal.task_count} task strings, "
            f'{share:.1%} of clustered episodes, {status}">'
            '<div class="de-class-size-head">'
            f'<span class="de-class-rank">{rank:02d}</span>'
            f"<strong>{escape(label)}</strong>"
            f'<span class="de-tag">{status}</span></div>'
            '<div class="de-class-track" aria-hidden="true">'
            f'<span class="de-class-bar" style="--de-class-width:{relative:.3f}%"></span></div>'
            '<div class="de-class-stats">'
            f"<span><strong>{proposal.size:,}</strong> episodes</span>"
            f"<span><strong>{proposal.task_count:,}</strong> distinct task strings</span>"
            f"<span><strong>{share:.1%}</strong> of clustered episodes</span>"
            "</div></li>"
        )
    return (
        f'<p class="de-dim">{len(ordered)} proposed classes · {total:,} episodes shown. '
        "Bar length compares each class with the largest proposal. Counts are exact; "
        "proposals are not confirmed semantic classes. "
        "Share is of the episodes represented here.</p>"
        '<ol class="de-class-size" tabindex="0" role="list" '
        'aria-label="Proposed classes ranked by episode count">' + "".join(rows) + "</ol>"
    )


def health_table(proposals: ProposalSet) -> str:
    """The numbers first. A reviewer should not have to infer health from the view."""
    health = proposals.health()
    cells = [
        ("task strings", health["tasks"]),
        ("episodes", health["episodes"]),
        ("proposals", health["clusters"]),
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
    """One proposal as the confirmation table renders it."""

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
    """Task strings behind a proposal, biggest first."""
    return [(member.task, member.episodes) for member in proposal.members]
