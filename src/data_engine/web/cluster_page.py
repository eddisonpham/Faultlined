"""The Clusters page's review and proposal components (ADR 0027)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from typing import Any

from data_engine.clustering import AXES, ProposalSet
from data_engine.web.clusters import (
    class_size_view,
    health_table,
    member_rows,
    proposal_rows,
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
                members=tuple(member_rows(proposal)),
                verbs=tuple(proposal.verbs().most_common(4)),
                colours=tuple(proposal.colours().most_common(4)),
            )
        )
    return out


def interpretation(proposals: ProposalSet) -> str:
    """Explain the run's measured shape without implying semantic correctness."""
    health = proposals.health()
    tasks = int(health["tasks"])
    clusters = int(health["clusters"])
    if not tasks:
        return "Nothing to propose over: the catalog has no task strings yet."
    ratio = clusters / tasks
    if tasks < 5:
        return (
            f"{clusters} cluster{'s' if clusters != 1 else ''} from {tasks} distinct task "
            "string(s). Too few to judge: ingest a corpus before reading anything into this run."
        )
    if ratio > 0.75:
        verdict = (
            f"Fragmenting: {clusters} clusters for {tasks} distinct task strings. "
            "Check verb coverage and extraction axes before confirming groups."
        )
    elif ratio < 0.35:
        verdict = (
            f"Merging: {clusters} clusters for {tasks} distinct task strings. "
            "Inspect merged cores before confirming a group."
        )
    else:
        verdict = (
            f"{clusters} clusters over {tasks} distinct task strings, with "
            f"{proposals.singletons} singletons and {proposals.merges} merged cores. "
            "Confirm only groups that hold up against their example strings."
        )
    if float(health["verb_rate"]) < 0.8:
        verdict += (
            f" Only {float(health['verb_rate']) * 100:.0f}% of strings had a recognised "
            "verb, so their cores still contain a verb phrase."
        )
    return verdict


def has_run(model: dict[str, Any]) -> bool:
    """A run exists when a proposal has been stored, not when its wrapper is truthy."""
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


def controls(proposals: ProposalSet, *, error: str = "") -> str:
    """Recompute options remain a real, accessible no-JavaScript form."""
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
    banner = f'<p class="de-error" role="alert">{escape(error)}</p>' if error else ""
    return (
        f"{banner}"
        '<form class="de-controls" method="post" action="/ui/clusters/rebuild">'
        f"{boxes}"
        '<label class="de-field"><span class="de-field-label">radius</span>'
        f'<input name="radius" type="text" value="{escape(str(proposals.radius))}" '
        'size="6"></label>'
        '<label class="de-field"><span class="de-field-label">rule</span>'
        f'<select name="rule">{rules}</select></label>'
        '<label class="de-field"><span class="de-field-label">source</span>'
        f'<select name="source">{sources}</select></label>'
        '<button type="submit" class="de-button">rebuild</button>'
        "</form>"
    )


def figures_section(proposals: ProposalSet) -> str:
    return class_size_view(proposals.proposals)


def review_section(
    candidates: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    *,
    error: str = "",
) -> str:
    """A bounded human queue; the signal is stated as evidence, never probability."""
    if candidates:
        rows = "".join(
            "<tr>"
            f'<td><input type="checkbox" name="task" value="{escape(str(item["task"]))}" '
            f'aria-label="review {escape(str(item["task"]))}"></td>'
            f"<td>{escape(str(item['task']))}</td>"
            f"<td>{escape(str(item['reason']))}</td>"
            f'<td class="num">{int(item["episodes"])}</td>'
            f'<td class="num">{float(item["distance"]):.3f} / {float(item["radius"]):.3f}</td>'
            f"<td>{escape(str(item['proposal_core']))}</td>"
            "</tr>"
            for item in candidates[:100]
        )
        queue = (
            (f'<p class="de-error" role="alert">{escape(error)}</p>' if error else "")
            + '<form method="post" action="/ui/clusters/review" class="de-review-form">'
            + '<fieldset class="de-review-selection">'
            + '<legend class="de-sr">Select task strings to review</legend>'
            + '<div class="de-table-wrap" tabindex="0" role="region" '
            + 'aria-label="uncertain task strings">'
            + '<table class="de-table">'
            + '<caption class="de-sr">Task strings for human review</caption>'
            + "<thead><tr><th>select</th><th>task string</th><th>signal</th>"
            + '<th class="num">episodes</th><th class="num">distance / radius</th>'
            + "<th>current core</th></tr></thead>"
            + f"<tbody>{rows}</tbody></table></div></fieldset>"
            + '<label class="de-field"><span class="de-field-label">human class name</span>'
            + '<input type="text" name="label" maxlength="120" autocomplete="off"></label>'
            + '<div class="de-review-actions">'
            + '<button class="de-button" type="submit" name="disposition" value="class">'
            + "create human class</button>"
            + '<button class="de-button de-button-quiet" type="submit" '
            + 'name="disposition" value="dismissed">'
            + "not a useful class</button></div>"
            + '<p class="de-dim">Select up to 25 strings. A human class is an annotation; '
            + "it does not rewrite episode metadata or automatic proposals. "
            + "Distance is a review heuristic, not confidence.</p>"
            + "</form>"
        )
    else:
        queue = (
            (f'<p class="de-error" role="alert">{escape(error)}</p>' if error else "")
            + '<p class="de-empty">// no unresolved singleton or near-boundary strings '
            + "in this run</p>"
        )
    if not decisions:
        return queue
    history_rows = "".join(
        "<tr>"
        f"<td>{escape(str(item['task']))}</td>"
        f"<td>{escape(str(item['disposition']))}</td>"
        f"<td>{escape(str(item['label']) or '—')}</td>"
        '<td><form method="post" action="/ui/clusters/review/undo">'
        f'<input type="hidden" name="task" value="{escape(str(item["task"]))}">'
        '<button class="de-button de-button-quiet" type="submit">reopen</button></form></td>'
        "</tr>"
        for item in decisions[:30]
    )
    history = (
        '<details class="de-review-history"><summary>Recent human decisions</summary>'
        '<div class="de-table-wrap" tabindex="0" role="region" '
        'aria-label="recent human review decisions">'
        '<table class="de-table"><caption class="de-sr">Recent human review decisions</caption>'
        "<thead><tr><th>task string</th><th>decision</th><th>class</th><th>action</th></tr></thead>"
        f"<tbody>{history_rows}</tbody></table></div></details>"
    )
    return '<div class="de-review">' + queue + history + "</div>"


MEMBERS_SHOWN = 4


def proposal_table(proposals: ProposalSet, *, read_only: bool = False) -> str:
    """Show compact examples and exact counts; preview data cannot be mutated."""
    rows: list[str] = []
    for row in rows_for(proposals):
        shown = row.members[:MEMBERS_SHOWN]
        tasks = "<br>".join(
            f'<span class="de-dim">{escape(str(count))}</span> {escape(task[:70])}'
            for task, count in shown
        )
        if len(row.members) > len(shown):
            hidden = len(row.members) - len(shown)
            tasks += f'<br><a href="/ui/clusters/{escape(row.key)}">and {hidden} more</a>'
        flags = []
        if row.frozen:
            flags.append('<span class="de-tag">confirmed</span>')
        if row.merged > 1:
            flags.append(f'<span class="de-tag de-tag-warn">{row.merged} cores merged</span>')
        if row.tasks == 1:
            flags.append('<span class="de-tag de-tag-warn">single task</span>')
        if read_only:
            control = '<span class="de-dim">preview only</span>'
        elif row.frozen:
            control = (
                f'<form method="post" action="/ui/clusters/{escape(row.key)}/release">'
                '<button type="submit" class="de-button">release</button></form>'
            )
        else:
            control = (
                f'<form method="post" action="/ui/clusters/{escape(row.key)}/confirm">'
                f'<input type="text" name="label" value="{escape(row.name)}" size="30" '
                f'aria-label="label for cluster {escape(row.key[:10])}">'
                '<button type="submit" class="de-button">confirm</button></form>'
            )
        rows.append(
            "<tr>"
            f'<td><span class="de-dot" style="background:{escape(row.colour)}"></span> '
            f"<code>{escape(row.key[:10])}</code></td>"
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
        '<table class="de-table">'
        '<caption class="de-sr">Automatic cluster proposals for human confirmation</caption>'
        "<thead><tr>"
        '<th>key</th><th>cluster</th><th class="num">episodes</th>'
        '<th class="num">tasks</th><th>flags</th><th>task strings</th><th>confirm</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        f'<p class="de-dim">Posting a label confirms the cluster and freezes it; the '
        f"confirmation is keyed by the cluster's content hash, so it survives a rebuild. "
        f"Members are the task strings behind the core, biggest episode count first.</p>"
    )


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
        '<table class="de-table">'
        '<caption class="de-sr">Clustering runs, newest first</caption><thead><tr>'
        '<th class="num">run</th><th>source</th><th>ignored</th><th class="num">radius</th>'
        '<th>rule</th><th class="num">clusters</th><th>when</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def sample_note() -> str:
    from data_engine.clustering import sample_data

    return f'<p class="de-warn">{escape(sample_data.SAMPLE_NOTE)}</p>'


def detail_tiles(proposal: Mapping[str, Any]) -> str:
    """Identity, volume, and freeze state for one proposal, in the health style."""
    label = str(proposal.get("label", ""))
    merged = [str(core) for core in (proposal.get("merged_cores") or [])]
    cells = [
        ("key", str(proposal.get("key", ""))[:12]),
        ("core", str(proposal.get("core", ""))),
        ("status", f"confirmed: {label}" if label else "proposal"),
        ("episodes", int(proposal.get("episodes", 0))),
        ("task strings", int(proposal.get("task_count", 0))),
        ("merged cores", len(merged)),
    ]
    tiles = "".join(
        f'<div class="de-stat"><span class="de-stat-value">{escape(str(value))}</span>'
        f'<span class="de-stat-label">{escape(name)}</span></div>'
        for name, value in cells
    )
    return f'<div class="de-stat-row">{tiles}</div>'


def detail_members(members: Sequence[Mapping[str, Any]]) -> str:
    """Every task string behind the proposal, with its extraction facts."""
    if not members:
        return '<p class="de-empty">// no task strings recorded for this proposal</p>'
    rows = "".join(
        "<tr>"
        f"<td>{escape(str(member['task']))}</td>"
        f"<td>{escape(str(member['core']))}</td>"
        f"<td>{escape(str(member.get('verb') or '') or chr(8212))}</td>"
        f"<td>{escape(', '.join(str(c) for c in (member.get('colours') or [])) or chr(8212))}</td>"
        f'<td class="num">{int(member["episodes"])}</td>'
        "</tr>"
        for member in members
    )
    return (
        '<div class="de-table-wrap" tabindex="0" role="region" aria-label="cluster members">'
        '<table class="de-table">'
        '<caption class="de-sr">Task strings grouped under this proposal</caption>'
        "<thead><tr><th>task string</th><th>object core</th><th>verb</th>"
        '<th>colours</th><th class="num">episodes</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
        f'<p class="de-dim">{len(members)} task strings. Cores and verbs are what the '
        "lexicon extracted, not ground truth; episode counts are exact.</p>"
    )


def detail_control(proposal: Mapping[str, Any], *, read_only: bool = False, error: str = "") -> str:
    """Confirm or release this proposal without leaving the page."""
    key = str(proposal.get("key", ""))
    label = str(proposal.get("label", ""))
    banner = f'<p class="de-error" role="alert">{escape(error)}</p>' if error else ""
    if read_only:
        return banner + '<span class="de-dim">preview only</span>'
    if label:
        control = (
            f'<form method="post" action="/ui/clusters/{escape(key)}/release">'
            '<button type="submit" class="de-button">release confirmation</button></form>'
        )
    else:
        control = (
            f'<form method="post" action="/ui/clusters/{escape(key)}/confirm" '
            'class="de-review-form">'
            '<label class="de-field"><span class="de-field-label">human class name</span>'
            f'<input type="text" name="label" value="{escape(str(proposal.get("core", "")))}" '
            f'size="30" aria-label="label for cluster {escape(key[:10])}"></label>'
            '<button type="submit" class="de-button">confirm</button></form>'
        )
    return banner + control


def detail_body(
    proposal: Mapping[str, Any],
    members: Sequence[Mapping[str, Any]],
    *,
    read_only: bool = False,
    error: str = "",
) -> str:
    """One proposal with everything a reviewer needs on one page (ADR 0027)."""
    merged = [str(core) for core in (proposal.get("merged_cores") or [])]
    merged_note = (
        f'<p class="de-dim">merged cores: {escape(", ".join(merged))}</p>' if merged else ""
    )
    total = int(proposal.get("task_count", len(members)))
    cap_note = (
        f'<p class="de-dim">showing the first {len(members)} of {total} task strings; '
        "the API serves the rest.</p>"
        if len(members) < total
        else ""
    )
    return (
        '<p><a href="/ui/clusters">&larr; all clusters</a></p>'
        + detail_tiles(proposal)
        + merged_note
        + '<div class="de-review">'
        + detail_members(members)
        + cap_note
        + detail_control(proposal, read_only=read_only, error=error)
        + "</div>"
    )
