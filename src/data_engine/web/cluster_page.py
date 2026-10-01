"""The Clusters page, composed.

Kept out of `pages.py` because this is the only page that is a *review* surface rather
than a read-only mirror: it has controls, it explains its own numbers, and it is the
place a human decides that a group of task strings is real. That is a different shape of
page from the other ten and putting it in the same 2100-line module would bury it.

The figures come from `web/clusters.py`. This module decides what the reader is told, and
in what order: health first (is this run worth looking at), then the two figures, then
the proposals with their controls, then the run history. A reader who stops after the
first section should still know whether to trust the rest.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from data_engine.clustering import AXES, ProposalSet
from data_engine.web.clusters import (
    cluster_colour,
    cluster_map,
    empty_map,
    health_table,
    layout_note,
    member_rows,
    proposal_rows,
    treemap,
)


@dataclass(frozen=True, slots=True)
class Row:
    """One proposal as the table renders it."""

    key: str
    name: str
    episodes: int
    tasks: int
    frozen: bool
    colour: str
    merged: int
    core: str
    members: tuple[tuple[str, int], ...]
    verbs: tuple[tuple[str, int], ...]
    colours: tuple[tuple[str, int], ...]


def rows_for(proposals: ProposalSet) -> list[Row]:
    out: list[Row] = []
    for summary in proposal_rows(proposals):
        proposal = next(item for item in proposals.proposals if item.key == summary.key)
        out.append(
            Row(
                key=proposal.key,
                name=proposal.label or proposal.core,
                episodes=proposal.size,
                tasks=proposal.task_count,
                frozen=proposal.frozen,
                colour=summary.colour,
                merged=summary.merged,
                core=proposal.core,
                members=tuple(member_rows(proposal)[:8]),
                verbs=tuple(proposal.verbs().most_common(4)),
                colours=tuple(proposal.colours().most_common(4)),
            )
        )
    return out


# --------------------------------------------------------------------- health


def interpretation(proposals: ProposalSet) -> str:
    """One sentence saying whether to trust this run, derived from its own numbers.

    Written rather than styled because the two failure modes pull in opposite
    directions and a reader needs to know which one they are looking at: too few
    clusters means merging, too many means fragmentation, and both look like a
    successful page.
    """
    health = proposals.health()
    tasks = int(health["tasks"])
    clusters = int(health["clusters"])
    if not tasks:
        return "Nothing to propose over: the catalog has no task strings yet."
    ratio = clusters / tasks if tasks else 0.0
    if tasks < 5:
        return (
            f"{clusters} cluster{'s' if clusters != 1 else ''} from {tasks} distinct task "
            "string(s). Too few to judge: ingest a corpus before reading anything into "
            "this run."
        )
    if ratio > 0.75:
        verdict = (
            f"Fragmenting: {clusters} clusters for {tasks} distinct task strings. "
            "Each string is nearly its own cluster, which is what the sentence-embedding "
            "pipeline did at scale (EXP-2.5-08). Check the verb coverage below, then "
            "consider ignoring an axis."
        )
    elif ratio < 0.35:
        verdict = (
            f"Merging: {clusters} clusters for {tasks} distinct task strings. Groups are "
            "larger than the variation in the corpus justifies; look for a proposal whose "
            "cores are unrelated before confirming it."
        )
    else:
        verdict = (
            f"{clusters} clusters over {tasks} distinct task strings, with "
            f"{proposals.singletons} singletons and {proposals.merges} merged cores. "
            "Confirm the ones that are real groups; leave the rest for the next rebuild."
        )
    if float(health["verb_rate"]) < 0.8:
        verdict += (
            f" Only {float(health['verb_rate']) * 100:.0f}% of strings had a recognised "
            "verb, so their cores still contain a verb phrase."
        )
    return verdict


def has_run(model: dict[str, Any]) -> bool:
    """Whether a clustering run has produced anything yet.

    `ProposalSet` is always truthy - it is a dataclass with no `__len__` - so the page
    has to look inside it. Getting this wrong renders a healthy-looking zero-filled
    dashboard on a catalog that has never been clustered.
    """
    proposals = model.get("proposals")
    return bool(proposals is not None and proposals.proposals)


def health_section(proposals: ProposalSet) -> str:
    health = proposals.health()
    detail = " ".join(
        [
            f"source={health['source']}",
            f"ignored={health['ignored']}",
            f"radius={health['radius']}",
            f"rule={health['rule']}",
            f"cores={health['distinct_cores']}",
        ]
    )
    return (
        health_table(proposals)
        + f'<p class="de-note">{escape(interpretation(proposals))}</p>'
        + f'<p class="de-dim">{escape(detail)}</p>'
    )


# ------------------------------------------------------------------- controls


def controls(proposals: ProposalSet, *, error: str = "") -> str:
    """Recompute and confirm, as plain form posts.

    No JavaScript: a control that only works with scripting on is a control that fails
    silently in the one situation an operator needs it - a browser with a blocked script,
    or a page that half-loaded.
    """
    ignored = set(proposals.ignored.names())
    boxes = "".join(
        f'<label class="de-check"><input type="checkbox" name="ignore" value="{escape(axis)}"'
        f"{' checked' if axis in ignored else ''}> ignore {escape(axis)}</label>"
        for axis in AXES
    )
    rules = "".join(
        f'<option value="{escape(name)}"'
        f"{' selected' if name == proposals.rule else ''}>{escape(name)}</option>"
        for name in ("running_mean", "sliding_8", "sliding_32", "ema_0.98")
    )
    sources = "".join(
        f'<option value="{escape(name)}"'
        f"{' selected' if name == proposals.source else ''}>{escape(name)}</option>"
        for name in ("catalog", "sample")
    )
    banner = f'<p class="de-error">{escape(error)}</p>' if error else ""
    return (
        f"{banner}"
        '<form class="de-controls" method="post" action="/ui/clusters/rebuild">'
        f"{boxes}"
        '<label class="de-field"><span class="de-field-label">radius</span>'
        f'<input name="radius" type="text" value="{escape(str(proposals.radius))}"'
        ' size="6"></label>'
        f'<label class="de-field"><span class="de-field-label">rule</span>'
        f'<select name="rule">{rules}</select></label>'
        '<label class="de-field"><span class="de-field-label">source</span>'
        f'<select name="source">{sources}</select></label>'
        '<button type="submit" class="de-button">rebuild</button>'
        "</form>"
    )


# -------------------------------------------------------------------- figures


def figures_section(proposals: ProposalSet) -> str:
    if not proposals.proposals:
        return empty_map(message="no proposals yet // rebuild to group the catalog's tasks")
    return (
        f"<p>{treemap(proposals.proposals)}</p>"
        f"{colour_legend(proposals)}"
        f"<p>{cluster_map(proposals)}</p>"
        f'<p class="de-dim">{escape(layout_note())}</p>'
    )


# ------------------------------------------------------------------- proposals


def _swatches(row: Row) -> str:
    return (
        f'<span class="de-dot" style="background:{escape(row.colour)}" '
        f'aria-hidden="true"></span><code>{escape(row.key[:10])}</code>'
    )


MEMBERS_SHOWN = 4


def proposal_table(proposals: ProposalSet) -> str:
    """One row per cluster: big enough to confirm from, and no taller than its neighbours.

    A cluster of eight merged cores used to list all eight task strings, which made one
    row taller than the rest of the table. Four plus a count and a link to the full member
    list says the same thing and keeps the rows comparable at a glance.
    """
    rows: list[str] = []
    for row in rows_for(proposals):
        shown = row.members[:MEMBERS_SHOWN]
        tasks = "<br>".join(
            f'<span class="de-dim">{escape(str(count))}</span> {escape(task[:70])}'
            for task, count in shown
        )
        if len(row.members) > len(shown):
            hidden = len(row.members) - len(shown)
            tasks += f'<br><a href="/api/v1/clusters/{escape(row.key)}">and {hidden} more</a>'
        flags = []
        if row.frozen:
            flags.append('<span class="de-tag">confirmed</span>')
        if row.merged > 1:
            flags.append(f'<span class="de-tag de-tag-warn">{row.merged} cores merged</span>')
        if row.tasks == 1:
            flags.append('<span class="de-tag de-tag-warn">single task</span>')
        control = (
            f'<form method="post" action="/ui/clusters/{escape(row.key)}/confirm">'
            f'<input type="text" name="label" value="{escape(row.name)}" size="30" '
            f'aria-label="label for cluster {escape(row.key[:10])}">'
            f'<button type="submit" class="de-button">'
            f"{'rename' if row.frozen else 'confirm'}</button></form>"
            if not row.frozen
            else f'<form method="post" action="/ui/clusters/{escape(row.key)}/release">'
            f'<button type="submit" class="de-button">release</button></form>'
        )
        rows.append(
            "<tr>"
            f"<td>{_swatches(row)}</td>"
            f"<td>{escape(row.name)}</td>"
            f'<td class="num">{row.episodes}</td>'
            f'<td class="num">{row.tasks}</td>'
            f"<td>{' '.join(flags)}</td>"
            f"<td>{tasks}</td>"
            f"<td>{control}</td>"
            "</tr>"
        )
    return (
        '<div class="de-table-wrap" tabindex="0" role="region" aria-label="cluster proposals">'
        '<table class="de-table"><thead><tr>'
        '<th>key</th><th>cluster</th><th class="num">episodes</th>'
        '<th class="num">tasks</th><th>flags</th><th>task strings</th><th>confirm</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        f'<p class="de-dim">Posting a label confirms the cluster and freezes it; the '
        f"confirmation is keyed by the cluster's content hash, so it survives a rebuild. "
        f"Members are the task strings behind the core, biggest episode count first.</p>"
    )


# ---------------------------------------------------------------------- history


def history_section(runs: Sequence[dict[str, Any]]) -> str:
    if not runs:
        return '<p class="de-empty">// no clustering runs recorded</p>'
    rows = "".join(
        "<tr>"
        f'<td class="num">{escape(str(run.get("id", "")))}</td>'
        f"<td>{escape(str(run.get('source', '')))}</td>"
        f"<td>{escape(str(run.get('ignored', '')))}</td>"
        f'<td class="num">{escape(str(run.get("radius", "")))}</td>'
        f"<td>{escape(str(run.get('rule', '')))}</td>"
        f"<td>{escape(str((run.get('health') or {}).get('clusters', '')))}</td>"
        f'<td class="dim">{escape(str(run.get("created_at", "")))}</td>'
        "</tr>"
        for run in runs
    )
    return (
        '<div class="de-table-wrap" tabindex="0" role="region" aria-label="clustering runs">'
        '<table class="de-table"><thead><tr><th class="num">run</th><th>source</th>'
        '<th>ignored</th><th class="num">radius</th><th>rule</th>'
        '<th class="num">clusters</th><th>when</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def sample_note() -> str:
    from data_engine.clustering import sample_data

    return f'<p class="de-warn">{escape(sample_data.SAMPLE_NOTE)}</p>'


LEGEND_SHOWN = 16


def colour_legend(proposals: ProposalSet) -> str:
    """The colour key, next to the treemap that uses it.

    Colour is a hash of the cluster key, so a cluster keeps its colour across rebuilds and
    the swatch in the proposals table means the same thing in both figures. Without the key
    the colours look like decoration and the two figures look unrelated.
    """
    ordered = proposals.ordered()
    items = "".join(
        f'<span class="de-chip" title="{escape(proposal.label or proposal.core)}">'
        f'<span class="de-dot" style="background:{escape(cluster_colour(proposal.key))}"></span>'
        f"{escape((proposal.label or proposal.core)[:28])}</span>"
        for proposal in ordered[:LEGEND_SHOWN]
    )
    if not items:
        return ""
    rest = len(ordered) - LEGEND_SHOWN
    tail = f'<span class="de-dim">and {rest} more in the table</span>' if rest > 0 else ""
    return (
        '<p class="de-dim">colour follows the cluster key, so it is stable across '
        f'rebuilds:</p><p class="de-chips">{items}{tail}</p>'
    )
