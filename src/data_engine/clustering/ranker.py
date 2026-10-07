"""Synonym-candidate ranking for the unmapped queue (ADR 0029 §5)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from data_engine.clustering.extract import extract


@dataclass(frozen=True, slots=True)
class Candidate:
    """One ranked suggestion for a group of unmapped strings."""

    kind: Literal["attach", "new_entry"]
    core: str
    task_strings: tuple[str, ...]
    episodes: int
    entry_id: str | None = None
    entry_label: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "core": self.core,
            "task_strings": list(self.task_strings),
            "episodes": self.episodes,
            "entry_id": self.entry_id,
            "entry_label": self.entry_label,
        }


def candidates(
    unmapped: Sequence[Mapping[str, object]],
    entries: Sequence[Mapping[str, object]],
) -> list[Candidate]:
    """Rank the unmapped strings into suggestions, most fragmenting first."""
    groups: dict[str, list[Mapping[str, object]]] = {}
    for row in unmapped:
        task = str(row.get("task_string") or "")
        core = extract(task).core
        if core:
            groups.setdefault(core, []).append(row)

    by_core: dict[str, list[Mapping[str, object]]] = {}
    for entry in entries:
        core = str(entry.get("core") or "")
        if core:
            by_core.setdefault(core, []).append(entry)

    ranked: list[Candidate] = []
    for core, rows in groups.items():
        tasks = tuple(sorted(str(row.get("task_string") or "") for row in rows))
        episodes = sum(int(cast(int, row.get("episodes") or 0)) for row in rows)
        targets = by_core.get(core, [])
        if len(targets) == 1:
            target = targets[0]
            ranked.append(
                Candidate(
                    kind="attach",
                    core=core,
                    task_strings=tasks,
                    episodes=episodes,
                    entry_id=str(target.get("id") or ""),
                    entry_label=str(target.get("preferred_label") or ""),
                )
            )
        elif not targets:
            ranked.append(
                Candidate(kind="new_entry", core=core, task_strings=tasks, episodes=episodes)
            )
    ranked.sort(key=lambda c: (-c.episodes, c.core))
    return ranked
