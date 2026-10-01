"""Catalog storage for cluster proposals and the confirmations attached to them.

Separate from `repository.py` on purpose. That module is the vertical slice of jobs,
episodes and lineage; this one is a different concern with a different lifetime, and
putting it in the same 1900-line file would make both worse to read. The table
definitions live with the schema in `database.py`.

**A confirmation is a claim about task strings, not about a row.** That distinction is
the whole design. A proposal's key is a hash of its primary core, so changing the
extraction options re-keys every proposal - and the first version of this module pointed
`cluster_confirmations` at that key with `ON DELETE CASCADE`. Measured consequence: one
rebuild with a different axis set deleted every label an operator had written. The
tables now store the *task strings* a confirmation covers, and `reapply` re-attaches
each one to whichever proposal holds those strings after a rebuild. A confirmation whose
strings are no longer together is counted as orphaned and reported, never silently
reattached to the nearest cluster.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from psycopg.types.json import Jsonb

from data_engine.catalog.database import connect
from data_engine.clustering import ProposalSet
from data_engine.config import Settings

#: Catalog rows read per rebuild. The cap is reported in the run's health block rather
#: than hidden: a clustering over the first 5000 task strings looks identical to a
#: complete one unless somebody says otherwise.
TASK_LIMIT = 5000


def distinct_tasks(settings: Settings, *, limit: int = TASK_LIMIT) -> dict[str, int]:
    """Every distinct task string in the catalog, with its episode count.

    Bounded on purpose: a catalog with a million episodes should not turn one page into
    a million-row scan.
    """
    with connect(settings) as connection:
        rows = connection.execute(
            """SELECT coalesce(metadata->>'task', '') AS task, count(*) AS episodes
                 FROM episodes
                WHERE coalesce(metadata->>'task', '') <> ''
                GROUP BY 1
                ORDER BY episodes DESC, task
                LIMIT %s""",
            (limit,),
        ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["task"])] = int(cast(int, row["episodes"]))
    return counts


@dataclass(frozen=True, slots=True)
class Confirmation:
    """A human's claim that some task strings form one group."""

    label: str
    tasks: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.tasks:
            raise ValueError("a confirmation must cover at least one task string")


def confirmations(settings: Settings) -> list[Confirmation]:
    """Every stored confirmation, oldest first."""
    with connect(settings) as connection:
        rows = connection.execute(
            "SELECT label, tasks FROM cluster_confirmations ORDER BY id"
        ).fetchall()
    out: list[Confirmation] = []
    for row in rows:
        stored_tasks = cast(list[object], row["tasks"] or [])
        out.append(
            Confirmation(label=str(row["label"]), tasks=tuple(str(task) for task in stored_tasks))
        )
    return out


@dataclass(frozen=True, slots=True)
class Reapplied:
    """What a rebuild did to the confirmations that already existed."""

    frozen: int
    orphaned: int

    def as_dict(self) -> dict[str, int]:
        return {"frozen": self.frozen, "orphaned": self.orphaned}


def labels_for(members: Mapping[str, set[str]], stored: Sequence[Confirmation]) -> dict[str, str]:
    """The label each proposal inherits from the confirmations covering its tasks.

    One function, used by both the rebuild and the page read, because two matching rules
    would eventually disagree and the reader would see a confirmed cluster counted as
    unconfirmed (or worse, the reverse).

    A proposal inherits a label only if it holds **more than half** of the confirmation's
    strings. Matching on any overlap at all was measured wrong: shrink the radius until a
    confirmed pair of task strings scatters into two singletons, and each singleton
    overlaps by one, the tie is broken on the key, and the operator's name lands on an
    arbitrary neighbouring cluster. That is the exact failure this module exists to
    avoid - a claim silently moving to something it was never about - so a confirmation
    only survives while its strings are still recognisably together.
    """
    labels: dict[str, str] = {}
    for entry in stored:
        wanted = set(entry.tasks)
        if not wanted:
            continue
        best: tuple[int, str] | None = None
        for key, tasks in members.items():
            overlap = len(wanted & tasks)
            if overlap * 2 > len(wanted) and (best is None or (overlap, key) > best):
                best = (overlap, key)
        if best is not None:
            labels[best[1]] = entry.label
    return labels


def reapply(stored: Sequence[Confirmation], proposals: ProposalSet) -> Reapplied:
    """Re-attach confirmations to the proposals that now hold their task strings.

    Matching is by overlap rather than by key, because a confirmation is about the
    strings and the group survives an option change even when its key does not. A
    confirmation whose strings no longer sit together in any one proposal is counted as
    orphaned, which is a fact the operator needs and not a rounding error to hide. See
    `labels_for` for why the threshold is a majority rather than any overlap.
    """
    members: dict[str, set[str]] = {
        proposal.key: {member.task for member in proposal.members}
        for proposal in proposals.proposals
    }
    labels = labels_for(members, stored)
    proposals.apply_confirmations(labels)
    return Reapplied(frozen=len(labels), orphaned=max(0, len(stored) - len(labels)))


def replace_proposals(settings: Settings, proposals: ProposalSet) -> dict[str, Any]:
    """Write one proposal run, replacing the previous proposals atomically.

    Confirmations are read and re-applied *before* the rows go, and they live in a table
    with no foreign key to `task_clusters`, so a rebuild cannot take them with it. One
    transaction means a crash mid-write leaves the previous run intact rather than a
    half-written mixture.
    """
    reapplied = reapply(confirmations(settings), proposals)
    with connect(settings) as connection, connection.transaction():
        connection.execute("DELETE FROM task_cluster_members")
        connection.execute("DELETE FROM task_clusters")
        for proposal in proposals.proposals:
            connection.execute(
                """INSERT INTO task_clusters
                           (key, core, episodes, task_count, merged_cores, centroid,
                            source, ignored, radius, rule)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    proposal.key,
                    proposal.core,
                    proposal.size,
                    proposal.task_count,
                    Jsonb(list(proposal.merged_cores)),
                    Jsonb(list(proposal.centroid)),
                    proposals.source,
                    proposals.ignored.describe(),
                    proposals.radius,
                    proposals.rule,
                ),
            )
            connection.cursor().executemany(
                """INSERT INTO task_cluster_members
                           (cluster_key, task, core, episodes, verb, colours)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (cluster_key, task) DO UPDATE
                           SET episodes = EXCLUDED.episodes""",
                [
                    (
                        proposal.key,
                        member.task,
                        member.core,
                        member.episodes,
                        member.verb,
                        Jsonb(list(member.colours)),
                    )
                    for member in proposal.members
                ],
            )
        run = connection.execute(
            """INSERT INTO cluster_runs (source, ignored, radius, rule, health)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at""",
            (
                proposals.source,
                proposals.ignored.describe(),
                proposals.radius,
                proposals.rule,
                Jsonb({**proposals.health(), **reapplied.as_dict()}),
            ),
        ).fetchone()
    if run is None:
        return {"run_id": 0, "created_at": None, **reapplied.as_dict()}
    return {
        "run_id": int(cast(int, run["id"])),
        "created_at": run["created_at"],
        **reapplied.as_dict(),
    }


def list_proposals(settings: Settings, *, limit: int = 200) -> list[dict[str, Any]]:
    """Proposals with their confirmation, biggest first.

    The confirmation is matched on the task strings the proposal holds, for the same
    reason `reapply` does: the stored key is historical and the strings are the claim.
    """
    stored = confirmations(settings)
    with connect(settings) as connection:
        rows = connection.execute(
            """SELECT c.key, c.core, c.episodes, c.task_count, c.merged_cores,
                      c.source, c.ignored, c.radius, c.rule, c.updated_at
                 FROM task_clusters c
                ORDER BY c.episodes DESC, c.core
                LIMIT %s""",
            (limit,),
        ).fetchall()
        member_rows = connection.execute(
            "SELECT cluster_key, task FROM task_cluster_members"
        ).fetchall()
    members: dict[str, set[str]] = {}
    for row in member_rows:
        members.setdefault(str(row["cluster_key"]), set()).add(str(row["task"]))

    labels = labels_for(members, stored)
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row["key"])
        label = labels.get(key, "")
        record = dict(row)
        record["label"] = label
        out.append(record)
    return out


def proposal_members(settings: Settings, key: str, *, limit: int = 200) -> list[dict[str, Any]]:
    with connect(settings) as connection:
        rows = connection.execute(
            """SELECT task, core, episodes, verb, colours
                 FROM task_cluster_members
                WHERE cluster_key = %s
                ORDER BY episodes DESC, task
                LIMIT %s""",
            (key, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_proposal(settings: Settings, key: str) -> dict[str, Any] | None:
    row = _row(settings, key)
    if row is None:
        return None
    row["label"] = labels_for({key: _tasks(settings, key)}, confirmations(settings)).get(key, "")
    return row


def _row(settings: Settings, key: str) -> dict[str, Any] | None:
    with connect(settings) as connection:
        row = connection.execute(
            """SELECT key, core, episodes, task_count, merged_cores, source, ignored,
                      radius, rule, updated_at
                 FROM task_clusters WHERE key = %s""",
            (key,),
        ).fetchone()
    return dict(row) if row else None


def _tasks(settings: Settings, key: str) -> set[str]:
    return {str(member["task"]) for member in proposal_members(settings, key, limit=1000)}


def confirm(settings: Settings, key: str, label: str, *, who: str = "operator") -> bool:
    """Confirm the proposal currently holding `key`, by its task strings.

    The task list is stored with the confirmation, so the claim outlives the row. Returns
    False when there is no such proposal or it holds no members, because a confirmation
    of nothing is not a confirmation.
    """
    cleaned = label.strip()
    if not cleaned:
        raise ValueError("a confirmation needs a label")
    tasks = sorted(_tasks(settings, key))
    if not tasks:
        return False
    with connect(settings) as connection:
        connection.execute("DELETE FROM cluster_confirmations WHERE tasks = %s", (Jsonb(tasks),))
        connection.execute(
            """INSERT INTO cluster_confirmations (label, tasks, confirmed_by)
               VALUES (%s, %s, %s)""",
            (cleaned, Jsonb(tasks), who),
        )
    return True


def unconfirm(settings: Settings, key: str) -> bool:
    """Release the confirmation covering this proposal's task strings."""
    tasks = sorted(_tasks(settings, key))
    if not tasks:
        return False
    with connect(settings) as connection:
        row = connection.execute(
            "DELETE FROM cluster_confirmations WHERE tasks = %s RETURNING id",
            (Jsonb(tasks),),
        ).fetchone()
    return row is not None


def latest_run(settings: Settings) -> dict[str, Any] | None:
    with connect(settings) as connection:
        row = connection.execute(
            """SELECT id, source, ignored, radius, rule, health, created_at
                 FROM cluster_runs ORDER BY created_at DESC, id DESC LIMIT 1"""
        ).fetchone()
    return dict(row) if row else None


def run_history(settings: Settings, *, limit: int = 20) -> list[dict[str, Any]]:
    with connect(settings) as connection:
        rows = connection.execute(
            """SELECT id, source, ignored, radius, rule, health, created_at
                 FROM cluster_runs ORDER BY created_at DESC, id DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def rebuild(
    settings: Settings,
    *,
    source: str = "catalog",
    ignored: str = "",
    radius: float = 0.30,
    rule: str = "running_mean",
    limit: int = TASK_LIMIT,
) -> dict[str, Any]:
    """Regroup the catalog's task strings and store the result.

    `source="sample"` proposes over the shipped LeRobot sentences instead, which is what
    makes the page demonstrate itself on a catalog with nothing ingested yet. Those runs
    are labelled `sample` in the stored health block so a later reader cannot mistake
    them for catalog statistics.
    """
    from data_engine.clustering import Ignored, build, sample_tasks

    if source == "sample":
        tasks = sample_tasks()
    else:
        tasks = distinct_tasks(settings, limit=limit)
    proposals = build(
        tasks,
        ignored=Ignored.parse(ignored),
        radius=radius,
        rule=rule,
        source=source,
    )
    stored = replace_proposals(settings, proposals)
    health = dict(proposals.health())
    health["task_limit"] = limit
    health["truncated"] = source != "sample" and len(tasks) >= limit
    health.update({key: stored[key] for key in ("frozen", "orphaned")})
    return {"health": health, "proposals": proposals, **stored}


def model(settings: Settings, *, limit: int = 200) -> dict[str, Any]:
    """Everything the Clusters page renders, in one read.

    Proposals come back from the catalog rather than being recomputed on the page view:
    a page load must not change the numbers it is showing, and recomputing would make
    every refresh a rebuild.
    """
    from data_engine.clustering import Ignored

    rows = list_proposals(settings, limit=limit)
    history = run_history(settings)
    if not rows:
        return {
            "rows": [],
            "members": {},
            "history": history,
            "run": None,
            "ignored": Ignored(),
            "radius": 0.30,
            "rule": "running_mean",
            "source": "catalog",
            "health": {},
        }
    latest = latest_run(settings)
    health = (latest or {}).get("health") or {}
    return {
        "rows": rows,
        "members": {row["key"]: proposal_members(settings, row["key"], limit=8) for row in rows},
        "history": history,
        "run": latest,
        "ignored": Ignored.parse(str(health.get("ignored", ""))),
        "radius": float(health.get("radius", 0.30)),
        "rule": str(health.get("rule", "running_mean")),
        "source": str(health.get("source", "catalog")),
        "health": health,
    }


def proposal_hash(core: str) -> str:
    """Re-exported so callers do not have to import two modules to build a key."""
    from data_engine.clustering import proposal_key

    return proposal_key(core)
