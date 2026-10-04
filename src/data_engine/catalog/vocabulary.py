"""Catalog storage for the task vocabulary (ADR 0029).

The vocabulary is the source of truth for what a task string means: entries with
human-approved preferred labels, and mappings from task strings onto them.
Episodes keep their raw task strings forever; everything here is a *view* over
them, so no vocabulary edit rewrites the audit trail.

Three properties the shapes below are chosen for:

- **Deterministic creation.** An entry id is `voc_<blake2b(label)>`, so creating
  is idempotent and the migration-0002 backfill is reproducible. The id names
  the entry, not the label: a rename keeps it.
- **A queue that cannot drift.** Unmapped strings are never written anywhere;
  the queue is a query over episodes minus mappings. A stale queue row is
  therefore impossible by construction.
- **Reversible operations.** Merge, split, rename, map and dismiss each record
  what they moved or replaced in `task_vocabulary_events`, and `undo_event` is a
  compensating action over that recorded payload - a merge can put a deleted
  entry back exactly as it was.

Separate from `repository.py` for the same reason `clusters.py` is: a different
concern with a different lifetime. Table definitions live in `database.py`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from psycopg import Connection
from psycopg.types.json import Jsonb

from data_engine.catalog.database import connect
from data_engine.clustering import extract
from data_engine.config import Settings

EVENT_KINDS = ("merge", "split", "label", "map", "dismiss")


class LabelConflict(ValueError):
    """A preferred label is already taken by another entry (409 at the API).

    A ValueError subclass so existing ValueError handling still applies, with
    the specific case named so the API can answer a conflict rather than a
    validation failure: the request is well-formed, the vocabulary just says no.
    """


#: How many episode-task rows a single queue read may consider. The unmapped
#: queue is bounded by design (ADR 0029 §9: >20% share is a falsifier, not a
#: backlog), and a bounded read keeps one page's cost independent of history.
TASK_LIMIT = 5000


def entry_id_for(preferred_label: str) -> str:
    """Content-derived entry id: deterministic at creation, stable forever after."""
    normalized = preferred_label.strip()
    digest = hashlib.blake2b(normalized.encode("utf-8"), digest_size=8).hexdigest()
    return f"voc_{digest}"


def core_of(task_string: str) -> str:
    """The extracted core of a task string - the measured grouping key (EXP-2.5-08)."""
    return extract(task_string).core


def _entry_row(row: dict[str, Any]) -> dict[str, Any]:
    created = row.get("created_at")
    return {
        "id": str(row["id"]),
        "preferred_label": str(row["preferred_label"]),
        "core": str(row.get("core") or ""),
        "notes": str(row.get("notes") or ""),
        "created_at": created.isoformat() if isinstance(created, datetime) else None,
        "created_by": str(row.get("created_by") or ""),
    }


def create_entry(
    settings: Settings,
    *,
    preferred_label: str,
    notes: str = "",
    core: str | None = None,
    actor: str = "operator",
) -> dict[str, Any]:
    """Create a vocabulary entry. The id derives from the label, so this is idempotent.

    A second create with the same label returns the existing entry rather than
    failing: a preferred label names exactly one entry by definition, and the
    caller who repeated the request meant the same thing.
    """
    label = preferred_label.strip()
    if not label:
        raise ValueError("a vocabulary entry needs a preferred label")
    entry_id = entry_id_for(label)
    resolved_core = core if core is not None else core_of(label)
    with connect(settings) as connection:
        row = connection.execute(
            """INSERT INTO task_vocabulary_entries
                   (id, preferred_label, core, notes, created_by)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING
               RETURNING *""",
            (entry_id, label, resolved_core, notes, actor),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT * FROM task_vocabulary_entries WHERE preferred_label = %s", (label,)
            ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT * FROM task_vocabulary_entries WHERE id = %s", (entry_id,)
            ).fetchone()
            if row is not None:
                raise LabelConflict(
                    f"entry id for {label!r} is already used by renamed entry "
                    f"{row['preferred_label']!r}"
                )
        connection.commit()
    if row is None:
        raise RuntimeError(f"entry {entry_id} vanished between insert and read")
    return _entry_row(row)


def get_entry(settings: Settings, entry_id: str) -> dict[str, Any] | None:
    with connect(settings) as connection:
        row = connection.execute(
            "SELECT * FROM task_vocabulary_entries WHERE id = %s", (entry_id,)
        ).fetchone()
    return None if row is None else _entry_row(row)


def list_entries(settings: Settings, *, limit: int = 200) -> list[dict[str, Any]]:
    """Every entry with how much of the catalog it explains, largest first."""
    with connect(settings) as connection:
        rows = connection.execute(
            """SELECT e.*, count(m.task_string) AS task_count,
                      coalesce(sum(t.episodes), 0) AS episodes
                 FROM task_vocabulary_entries e
                 LEFT JOIN task_vocabulary_mappings m ON m.entry_id = e.id
                 LEFT JOIN (SELECT coalesce(metadata->>'task', '') AS task,
                                   count(*) AS episodes
                              FROM episodes GROUP BY 1) t ON t.task = m.task_string
                GROUP BY e.id
                ORDER BY episodes DESC, e.preferred_label
                LIMIT %s""",
            (limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        entry = _entry_row(row)
        entry["task_count"] = int(cast(int, row.get("task_count") or 0))
        entry["episodes"] = int(cast(int, row.get("episodes") or 0))
        out.append(entry)
    return out


def entry_members(settings: Settings, entry_id: str) -> list[dict[str, Any]]:
    """The task strings mapped to one entry, with their episode counts."""
    with connect(settings) as connection:
        rows = connection.execute(
            """SELECT m.task_string, m.provenance, m.mapped_at,
                      coalesce(t.episodes, 0) AS episodes
                 FROM task_vocabulary_mappings m
                 LEFT JOIN (SELECT coalesce(metadata->>'task', '') AS task,
                                   count(*) AS episodes
                              FROM episodes GROUP BY 1) t ON t.task = m.task_string
                WHERE m.entry_id = %s
                ORDER BY episodes DESC, m.task_string""",
            (entry_id,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        mapped_at = row.get("mapped_at")
        out.append(
            {
                "task_string": str(row["task_string"]),
                "provenance": str(row["provenance"]),
                "episodes": int(cast(int, row.get("episodes") or 0)),
                "mapped_at": mapped_at.isoformat() if isinstance(mapped_at, datetime) else None,
            }
        )
    return out


def mapping_for(settings: Settings, task_string: str) -> dict[str, Any] | None:
    with connect(settings) as connection:
        row = connection.execute(
            "SELECT task_string, entry_id, provenance FROM task_vocabulary_mappings "
            "WHERE task_string = %s",
            (task_string,),
        ).fetchone()
    if row is None:
        return None
    return {
        "task_string": str(row["task_string"]),
        "entry_id": str(row["entry_id"]) if row.get("entry_id") is not None else None,
        "provenance": str(row["provenance"]),
    }


def map_task(
    settings: Settings,
    *,
    task_string: str,
    entry_id: str,
    provenance: str = "confirm",
    actor: str = "operator",
) -> dict[str, Any]:
    """Put a task string under an entry. Re-mapping is allowed: a human can fix a mistake."""
    if provenance == "dismiss":
        raise ValueError("dismiss is not a mapping onto an entry; use dismiss_task")
    if get_entry(settings, entry_id) is None:
        raise KeyError(entry_id)
    previous = mapping_for(settings, task_string)
    with connect(settings) as connection:
        connection.execute(
            """INSERT INTO task_vocabulary_mappings (task_string, entry_id, provenance)
               VALUES (%s, %s, %s)
               ON CONFLICT (task_string) DO UPDATE
                   SET entry_id = EXCLUDED.entry_id, provenance = EXCLUDED.provenance,
                       mapped_at = now()""",
            (task_string, entry_id, provenance),
        )
        connection.execute(
            """INSERT INTO task_vocabulary_events (kind, payload, created_by)
               VALUES ('map', %s, %s)""",
            (
                Jsonb(
                    {
                        "task_string": task_string,
                        "entry_id": entry_id,
                        "provenance": provenance,
                        "previous": previous,
                    }
                ),
                actor,
            ),
        )
        connection.commit()
    mapped = mapping_for(settings, task_string)
    if mapped is None:
        raise RuntimeError("mapping vanished between write and read")
    return mapped


def accept_candidate(
    settings: Settings,
    *,
    task_strings: Sequence[str],
    entry_id: str | None = None,
    new_label: str | None = None,
    expected_core: str,
    actor: str = "operator",
) -> dict[str, Any]:
    """Apply a reviewed candidate atomically, creating its entry if requested.

    The caller presents and revalidates the suggestion; this transaction checks
    that each selected string still exists and is still unmapped before writing
    any entry or mapping, so a stale form or database failure cannot leave half
    a candidate applied.
    """
    selected = list(dict.fromkeys(task_strings))
    if not selected or len(selected) > 500:
        raise ValueError("a candidate must contain between 1 and 500 task strings")
    label = (new_label or "").strip()
    # Exactly one target: an existing entry to attach to, or a new label to
    # create one from. Both or neither is a caller bug, not a judgment call.
    if (entry_id is not None) == bool(label):
        raise ValueError("choose exactly one existing entry or new label")

    with connect(settings) as connection:
        core_entries = connection.execute(
            "SELECT id FROM task_vocabulary_entries WHERE core = %s ORDER BY id FOR UPDATE",
            (expected_core,),
        ).fetchall()
        if entry_id is not None:
            entry_row = connection.execute(
                "SELECT * FROM task_vocabulary_entries WHERE id = %s FOR UPDATE", (entry_id,)
            ).fetchone()
            if entry_row is None:
                raise KeyError(entry_id)
            if [str(row["id"]) for row in core_entries] != [entry_id]:
                raise ValueError("candidate changed; the target is no longer the unique core match")
        else:
            if core_entries:
                raise ValueError("candidate changed; an entry now uses the suggested core")
            new_id = entry_id_for(label)
            resolved_core = core_of(label)
            entry_row = connection.execute(
                """INSERT INTO task_vocabulary_entries
                       (id, preferred_label, core, notes, created_by)
                   VALUES (%s, %s, %s, '', %s)
                   ON CONFLICT DO NOTHING
                   RETURNING *""",
                (new_id, label, resolved_core, actor),
            ).fetchone()
            if entry_row is None:
                entry_row = connection.execute(
                    "SELECT * FROM task_vocabulary_entries WHERE preferred_label = %s FOR UPDATE",
                    (label,),
                ).fetchone()
            if entry_row is None:
                entry_row = connection.execute(
                    "SELECT * FROM task_vocabulary_entries WHERE id = %s FOR UPDATE", (new_id,)
                ).fetchone()
                if entry_row is not None:
                    raise LabelConflict(
                        f"entry id for {label!r} is already used by renamed entry "
                        f"{entry_row['preferred_label']!r}"
                    )
            if entry_row is None:
                raise RuntimeError(f"entry {new_id} vanished between insert and read")

        found_rows = connection.execute(
            """SELECT DISTINCT metadata->>'task' AS task
                 FROM episodes
                WHERE metadata->>'task' = ANY(%s)""",
            (selected,),
        ).fetchall()
        found = {str(row["task"]) for row in found_rows}
        missing = [task for task in selected if task not in found]
        if missing:
            raise ValueError(
                f"candidate changed; task strings no longer exist: {', '.join(missing)}"
            )
        wrong_core = [task for task in selected if core_of(task) != expected_core]
        if wrong_core:
            raise ValueError(
                "candidate changed; task strings no longer share the suggested core: "
                + ", ".join(wrong_core)
            )
        mapped_rows = connection.execute(
            "SELECT task_string FROM task_vocabulary_mappings WHERE task_string = ANY(%s)",
            (selected,),
        ).fetchall()
        already_mapped = {str(row["task_string"]) for row in mapped_rows}
        if already_mapped:
            raise ValueError(
                "candidate changed; task strings are no longer unmapped: "
                + ", ".join(sorted(already_mapped))
            )

        target_id = str(entry_row["id"])
        # The entry a `new_entry` candidate created is recorded on each of its map
        # events, so undoing the acceptance removes the entry again. An `attach`
        # candidate creates nothing and records nothing to remove.
        created_entry_id = target_id if entry_id is None else None
        for task in selected:
            connection.execute(
                """INSERT INTO task_vocabulary_mappings (task_string, entry_id, provenance)
                   VALUES (%s, %s, 'confirm')""",
                (task, target_id),
            )
            connection.execute(
                """INSERT INTO task_vocabulary_events (kind, payload, created_by)
                   VALUES ('map', %s, %s)""",
                (
                    Jsonb(
                        {
                            "task_string": task,
                            "entry_id": target_id,
                            "provenance": "confirm",
                            "candidate": {
                                "task_strings": selected,
                                "created_entry_id": created_entry_id,
                            },
                        }
                    ),
                    actor,
                ),
            )
        connection.commit()
    return _entry_row(entry_row)


def dismiss_task(
    settings: Settings, *, task_string: str, actor: str = "operator"
) -> dict[str, Any]:
    """Mark a string as noise that must never re-queue (the old `dismissed` disposition)."""
    previous = mapping_for(settings, task_string)
    with connect(settings) as connection:
        connection.execute(
            """INSERT INTO task_vocabulary_mappings (task_string, entry_id, provenance)
               VALUES (%s, NULL, 'dismiss')
               ON CONFLICT (task_string) DO UPDATE
                   SET entry_id = NULL, provenance = 'dismiss', mapped_at = now()""",
            (task_string,),
        )
        connection.execute(
            """INSERT INTO task_vocabulary_events (kind, payload, created_by)
               VALUES ('dismiss', %s, %s)""",
            (Jsonb({"task_string": task_string, "previous": previous}), actor),
        )
        connection.commit()
    return {"task_string": task_string, "entry_id": None, "provenance": "dismiss"}


def map_or_resolve(settings: Settings, task_string: str) -> str:
    """Ingest-time resolution (ADR 0029 §3). Returns how the string resolved.

    `mapped` - already under an entry. `dismissed` - already judged noise.
    `auto` - its extracted core matched exactly one entry's core, so it is
    mapped now (provenance `ingest`). `unmapped` - novel or ambiguous; nothing
    is written, because the queue is a query and a queue row cannot go stale.
    """
    if not task_string.strip():
        return "dismissed"
    existing = mapping_for(settings, task_string)
    if existing is not None:
        return "dismissed" if existing["entry_id"] is None else "mapped"
    core = core_of(task_string)
    if core:
        with connect(settings) as connection:
            rows = connection.execute(
                "SELECT id FROM task_vocabulary_entries WHERE core = %s", (core,)
            ).fetchall()
        if len(rows) == 1:
            map_task(
                settings,
                task_string=task_string,
                entry_id=str(rows[0]["id"]),
                provenance="ingest",
                actor="ingest",
            )
            return "auto"
    return "unmapped"


def list_unmapped(
    settings: Settings,
    *,
    limit: int = 50,
    after: tuple[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Novel task strings, most fragmenting first. The bounded review queue.

    Ranked by episode count - the operator's next action should remove the most
    fragmentation - with the string as tiebreak. ``after`` continues from the
    previous page's last ``(episodes, task)`` pair.
    """
    cursor = ""
    params: list[Any] = []
    if after is not None:
        cursor = "WHERE q.episodes < %s OR (q.episodes = %s AND q.task > %s)"
        params.extend([after[0], after[0], after[1]])
    with connect(settings) as connection:
        rows = connection.execute(
            f"""SELECT * FROM (
                    SELECT coalesce(metadata->>'task', '') AS task,
                           count(*) AS episodes,
                           min(created_at) AS first_seen
                      FROM episodes
                     WHERE coalesce(metadata->>'task', '') <> ''
                       AND NOT EXISTS (
                            SELECT 1 FROM task_vocabulary_mappings m
                             WHERE m.task_string = coalesce(episodes.metadata->>'task', ''))
                     GROUP BY 1
                ) q {cursor}
                ORDER BY q.episodes DESC, q.task
                LIMIT %s""",
            (*params, min(limit, TASK_LIMIT)),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        first_seen = row.get("first_seen")
        out.append(
            {
                "task_string": str(row["task"]),
                "episodes": int(cast(int, row["episodes"])),
                "first_seen": first_seen.isoformat() if isinstance(first_seen, datetime) else None,
            }
        )
    return out


def unmapped_counts(settings: Settings) -> tuple[int, int, int]:
    """(unmapped strings, dismissed strings, mapped strings) over the catalog's tasks."""
    with connect(settings) as connection:
        row = connection.execute(
            """SELECT count(DISTINCT task) AS strings,
                      count(DISTINCT task) FILTER (
                          WHERE m.entry_id IS NOT NULL) AS mapped,
                      count(DISTINCT task) FILTER (
                          WHERE m.provenance = 'dismiss') AS dismissed
                 FROM (SELECT coalesce(metadata->>'task', '') AS task
                         FROM episodes WHERE coalesce(metadata->>'task', '') <> '') e
                 LEFT JOIN task_vocabulary_mappings m ON m.task_string = e.task"""
        ).fetchone()
    if row is None:
        return (0, 0, 0)
    total = int(cast(int, row["strings"] or 0))
    mapped = int(cast(int, row["mapped"] or 0))
    dismissed = int(cast(int, row["dismissed"] or 0))
    return (total - mapped - dismissed, dismissed, mapped)


def vocabulary_health(settings: Settings) -> dict[str, Any]:
    """The numbers the page leads with. Unmapped share is the headline.

    Both shares are published: a string-weighted share hides one very common
    unmapped string behind many rare mapped ones, and an episode-weighted share
    hides many rare gaps behind one common mapping. They disagree often enough
    that reporting one as *the* number would be a lie of presentation.
    """
    unmapped, dismissed, mapped = unmapped_counts(settings)
    strings = unmapped + dismissed + mapped
    with connect(settings) as connection:
        row = connection.execute(
            """SELECT count(*) AS episodes,
                      count(*) FILTER (WHERE m.entry_id IS NOT NULL) AS mapped,
                      count(*) FILTER (WHERE m.provenance = 'dismiss') AS dismissed,
                      count(*) FILTER (
                          WHERE coalesce(metadata->>'task', '') <> ''
                            AND m.task_string IS NULL) AS unmapped
                 FROM episodes e
                 LEFT JOIN task_vocabulary_mappings m
                        ON m.task_string = coalesce(e.metadata->>'task', '')"""
        ).fetchone()
        entries_row = connection.execute(
            "SELECT count(*) AS n FROM task_vocabulary_entries"
        ).fetchone()
        orphaned_row = connection.execute(
            """SELECT count(*) AS n
                 FROM task_vocabulary_mappings m
                WHERE m.entry_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM task_vocabulary_entries e WHERE e.id = m.entry_id)"""
        ).fetchone()
        entries = int(cast(int, entries_row["n"])) if entries_row else 0
        orphaned = int(cast(int, orphaned_row["n"])) if orphaned_row else 0
    episodes = int(cast(int, row["episodes"] or 0)) if row else 0
    ep_mapped = int(cast(int, row["mapped"] or 0)) if row else 0
    ep_dismissed = int(cast(int, row["dismissed"] or 0)) if row else 0
    ep_unmapped = int(cast(int, row["unmapped"] or 0)) if row else 0
    from data_engine.clustering.ranker import candidates

    pending_candidates = len(
        candidates(list_unmapped(settings, limit=TASK_LIMIT), list_entries(settings))
    )
    return {
        "entries": entries,
        "orphaned_mappings": orphaned,
        "pending_candidates": pending_candidates,
        "task_strings": strings,
        "mapped_strings": mapped,
        "dismissed_strings": dismissed,
        "unmapped_strings": unmapped,
        "unmapped_string_share": (unmapped / strings) if strings else 0.0,
        "episodes": episodes,
        "mapped_episodes": ep_mapped,
        "dismissed_episodes": ep_dismissed,
        "unmapped_episodes": ep_unmapped,
        "unmapped_episode_share": (ep_unmapped / episodes) if episodes else 0.0,
    }


def update_notes(settings: Settings, *, entry_id: str, notes: str) -> dict[str, Any]:
    """Edit an entry's notes. Wording, not structure: no event, nothing to undo."""
    if get_entry(settings, entry_id) is None:
        raise KeyError(entry_id)
    with connect(settings) as connection:
        connection.execute(
            "UPDATE task_vocabulary_entries SET notes = %s WHERE id = %s", (notes, entry_id)
        )
        connection.commit()
    updated = get_entry(settings, entry_id)
    if updated is None:
        raise RuntimeError("entry vanished between update and read")
    return updated


def rename_entry(
    settings: Settings, *, entry_id: str, preferred_label: str, actor: str = "operator"
) -> dict[str, Any]:
    """Rename an entry. The id does not follow the label - it names the entry (ADR 0029 §2)."""
    label = preferred_label.strip()
    if not label:
        raise ValueError("a vocabulary entry needs a preferred label")
    if get_entry(settings, entry_id) is None:
        raise KeyError(entry_id)
    with connect(settings) as connection:
        clash = connection.execute(
            "SELECT id FROM task_vocabulary_entries WHERE preferred_label = %s AND id <> %s",
            (label, entry_id),
        ).fetchone()
        if clash is not None:
            raise LabelConflict(f"preferred label already taken: {label}")
        entry_row = connection.execute(
            "SELECT preferred_label FROM task_vocabulary_entries WHERE id = %s FOR UPDATE",
            (entry_id,),
        ).fetchone()
        if entry_row is None:
            raise KeyError(entry_id)
        old_label = str(entry_row["preferred_label"])
        connection.execute(
            "UPDATE task_vocabulary_entries SET preferred_label = %s WHERE id = %s",
            (label, entry_id),
        )
        connection.execute(
            """INSERT INTO task_vocabulary_events (kind, payload, created_by)
               VALUES ('label', %s, %s)""",
            (
                Jsonb(
                    {
                        "entry_id": entry_id,
                        "old_label": old_label,
                        "new_label": label,
                    }
                ),
                actor,
            ),
        )
        connection.commit()
    updated = get_entry(settings, entry_id)
    if updated is None:
        raise RuntimeError("entry vanished between rename and read")
    return updated


def merge_entries(
    settings: Settings, *, source_id: str, target_id: str, actor: str = "operator"
) -> dict[str, Any]:
    """Merge one entry into another. The event records every string it moved.

    The source entry is deleted - its restore lives in the event payload, so an
    undo puts it back exactly. This is the visibility the old proposals lacked:
    the merge is an event with a name and a list, not an invisible union.
    """
    if source_id == target_id:
        raise ValueError("an entry cannot merge into itself")
    source = get_entry(settings, source_id)
    target = get_entry(settings, target_id)
    if source is None:
        raise KeyError(source_id)
    if target is None:
        raise KeyError(target_id)
    with connect(settings) as connection:
        rows = connection.execute(
            "SELECT task_string, provenance FROM task_vocabulary_mappings WHERE entry_id = %s",
            (source_id,),
        ).fetchall()
        moved = [
            {"task_string": str(r["task_string"]), "provenance": str(r["provenance"])} for r in rows
        ]
        connection.execute(
            "UPDATE task_vocabulary_mappings SET entry_id = %s, provenance = 'merge', "
            "mapped_at = now() WHERE entry_id = %s",
            (target_id, source_id),
        )
        connection.execute("DELETE FROM task_vocabulary_entries WHERE id = %s", (source_id,))
        event = connection.execute(
            """INSERT INTO task_vocabulary_events (kind, payload, created_by)
               VALUES ('merge', %s, %s) RETURNING id, created_at""",
            (
                Jsonb({"source": source, "target_id": target_id, "moved": moved}),
                actor,
            ),
        ).fetchone()
        connection.commit()
    return {
        "event_id": int(cast(int, event["id"])) if event else 0,
        "moved": len(moved),
        "source": source,
        "target_id": target_id,
    }


def split_entry(
    settings: Settings,
    *,
    entry_id: str,
    task_strings: Sequence[str],
    new_label: str,
    actor: str = "operator",
) -> dict[str, Any]:
    """Split some strings out of an entry into a new one. Recorded, reversible."""
    selected = list(dict.fromkeys(task_strings))
    if not selected:
        raise ValueError("a split must name at least one task string")
    label = new_label.strip()
    if not label:
        raise ValueError("a split needs a preferred label")
    new_entry_id = entry_id_for(label)
    new_core = core_of(label)
    with connect(settings) as connection:
        parent = connection.execute(
            "SELECT id FROM task_vocabulary_entries WHERE id = %s FOR UPDATE", (entry_id,)
        ).fetchone()
        if parent is None:
            raise KeyError(entry_id)
        rows = connection.execute(
            "SELECT task_string, provenance FROM task_vocabulary_mappings "
            "WHERE entry_id = %s AND task_string = ANY(%s) FOR UPDATE",
            (entry_id, selected),
        ).fetchall()
        found = {str(r["task_string"]): str(r["provenance"]) for r in rows}
        missing = [task for task in selected if task not in found]
        if missing:
            raise ValueError(f"not mapped to {entry_id}: {', '.join(missing)}")
        if new_entry_id == entry_id:
            raise ValueError("the split target already is this entry")
        clash = connection.execute(
            "SELECT id FROM task_vocabulary_entries WHERE preferred_label = %s", (label,)
        ).fetchone()
        if clash is not None:
            raise LabelConflict(f"preferred label already taken: {label}")
        inserted = connection.execute(
            """INSERT INTO task_vocabulary_entries
                   (id, preferred_label, core, notes, created_by)
               VALUES (%s, %s, %s, '', %s)
               RETURNING *""",
            (new_entry_id, label, new_core, actor),
        ).fetchone()
        if inserted is None:
            raise RuntimeError("split entry was not created")
        new_entry = _entry_row(inserted)
        moved = [{"task_string": task, "provenance": found[task]} for task in selected]
        connection.execute(
            "UPDATE task_vocabulary_mappings SET entry_id = %s, provenance = 'split', "
            "mapped_at = now() WHERE entry_id = %s AND task_string = ANY(%s)",
            (new_entry_id, entry_id, selected),
        )
        event = connection.execute(
            """INSERT INTO task_vocabulary_events (kind, payload, created_by)
               VALUES ('split', %s, %s) RETURNING id, created_at""",
            (
                Jsonb(
                    {
                        "entry_id": entry_id,
                        "new_entry_id": new_entry_id,
                        "new_label": label,
                        "moved": moved,
                    }
                ),
                actor,
            ),
        ).fetchone()
        connection.commit()
    return {
        "event_id": int(cast(int, event["id"])) if event else 0,
        "moved": len(moved),
        "entry_id": entry_id,
        "new_entry": new_entry,
    }


def list_events(settings: Settings, *, limit: int = 20) -> list[dict[str, Any]]:
    """Recent vocabulary operations, newest first - the undo surface's memory."""
    with connect(settings) as connection:
        rows = connection.execute(
            "SELECT id, kind, payload, created_at, created_by, undone_at "
            "FROM task_vocabulary_events ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        created = row.get("created_at")
        undone = row.get("undone_at")
        out.append(
            {
                "id": int(cast(int, row["id"])),
                "kind": str(row["kind"]),
                "payload": dict(cast(dict[str, Any], row.get("payload") or {})),
                "created_at": created.isoformat() if isinstance(created, datetime) else None,
                "created_by": str(row.get("created_by") or ""),
                "undone_at": undone.isoformat() if isinstance(undone, datetime) else None,
            }
        )
    return out


def _drop_unreferenced_entry(connection: Connection[Any], entry_id: str) -> None:
    """Delete an entry only while nothing maps to it any more.

    Undo runs one event at a time, so a multi-string candidate is undone by its
    last event; the entry has to survive the earlier ones and disappear on this
    one. A mapping added after the acceptance is a reason to keep it rather than
    to delete an entry something else still depends on.
    """
    still_mapped = connection.execute(
        "SELECT 1 FROM task_vocabulary_mappings WHERE entry_id = %s LIMIT 1", (entry_id,)
    ).fetchone()
    if still_mapped is None:
        connection.execute("DELETE FROM task_vocabulary_entries WHERE id = %s", (entry_id,))


def undo_event(settings: Settings, *, event_id: int, actor: str = "operator") -> dict[str, Any]:
    """Compensating action for one recorded event, restoring exactly what it changed."""
    with connect(settings) as connection:
        event_row = connection.execute(
            """SELECT id, kind, payload, undone_at FROM task_vocabulary_events
                 WHERE id = %s FOR UPDATE""",
            (event_id,),
        ).fetchone()
        if event_row is None:
            raise KeyError(event_id)
        if event_row.get("undone_at") is not None:
            raise ValueError("event already undone")
        kind = str(event_row["kind"])
        payload = cast(dict[str, Any], event_row["payload"] or {})
        if kind == "merge":
            source = cast(dict[str, Any], payload["source"])
            connection.execute(
                """INSERT INTO task_vocabulary_entries
                       (id, preferred_label, core, notes, created_at, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (
                    source["id"],
                    source["preferred_label"],
                    source.get("core") or "",
                    source.get("notes") or "",
                    source.get("created_at") or datetime.now(UTC),
                    source.get("created_by") or actor,
                ),
            )
            for moved in cast(list[dict[str, Any]], payload.get("moved") or []):
                connection.execute(
                    """INSERT INTO task_vocabulary_mappings
                           (task_string, entry_id, provenance)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (task_string) DO UPDATE
                           SET entry_id = EXCLUDED.entry_id,
                               provenance = EXCLUDED.provenance,
                               mapped_at = now()""",
                    (
                        moved["task_string"],
                        source["id"],
                        moved.get("provenance") or "confirm",
                    ),
                )
        elif kind == "split":
            for moved in cast(list[dict[str, Any]], payload.get("moved") or []):
                connection.execute(
                    """INSERT INTO task_vocabulary_mappings
                           (task_string, entry_id, provenance)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (task_string) DO UPDATE
                           SET entry_id = EXCLUDED.entry_id,
                               provenance = EXCLUDED.provenance,
                               mapped_at = now()""",
                    (
                        moved["task_string"],
                        payload["entry_id"],
                        moved.get("provenance") or "confirm",
                    ),
                )
            connection.execute(
                "DELETE FROM task_vocabulary_entries WHERE id = %s",
                (payload["new_entry_id"],),
            )
        elif kind == "label":
            connection.execute(
                "UPDATE task_vocabulary_entries SET preferred_label = %s WHERE id = %s",
                (payload["old_label"], payload["entry_id"]),
            )
        elif kind in ("map", "dismiss"):
            task = str(payload["task_string"])
            previous = cast(dict[str, Any] | None, payload.get("previous"))
            if previous is None:
                connection.execute(
                    "DELETE FROM task_vocabulary_mappings WHERE task_string = %s", (task,)
                )
            else:
                connection.execute(
                    """INSERT INTO task_vocabulary_mappings
                           (task_string, entry_id, provenance)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (task_string) DO UPDATE
                           SET entry_id = EXCLUDED.entry_id,
                               provenance = EXCLUDED.provenance,
                               mapped_at = now()""",
                    (task, previous.get("entry_id"), previous.get("provenance") or "confirm"),
                )
            candidate = cast(dict[str, Any], payload.get("candidate") or {})
            created = candidate.get("created_entry_id")
            if previous is None and created:
                _drop_unreferenced_entry(connection, str(created))
        else:
            raise ValueError(f"unknown event kind: {kind}")
        connection.execute(
            "UPDATE task_vocabulary_events SET undone_at = now() WHERE id = %s", (event_id,)
        )
        connection.commit()
    return {"event_id": event_id, "kind": kind, "undone": True}
