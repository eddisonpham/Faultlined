"""What a build contains, and what it lacks (ADR 0033)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "AXES",
    "GAP_LIMIT",
    "VALUE_LIMIT",
    "CoverageAxis",
    "CoverageReport",
    "CoverageValue",
    "coverage_report",
]

VALUE_LIMIT = 20

GAP_LIMIT = 20

AXES: tuple[tuple[str, str], ...] = (
    ("task", "Task"),
    ("robot", "Embodiment"),
    ("format", "Format"),
    ("verdict", "Verdict"),
)

_VOCABULARY_AXIS = "task"


@dataclass(frozen=True, slots=True)
class CoverageValue:
    """One value on one axis, with how much of the build it accounts for."""

    value: str
    count: int
    share: float
    """`count / episode_count`, or 0.0 for an empty build. """

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "count": self.count, "share": self.share}


@dataclass(frozen=True, slots=True)
class CoverageAxis:
    """One axis of a build's coverage: what it holds, and what the reference set holds instead."""

    name: str
    label: str
    present: int
    """Distinct values the build holds, before `VALUE_LIMIT` truncation. """

    values: tuple[CoverageValue, ...]
    gaps: tuple[str, ...]
    """Values the reference set holds and the build does not, alphabetical, truncated to
    `GAP_LIMIT`.
    """

    missing: int
    """How many gaps there are before truncation, so a capped list says "at least"."""

    truncated: bool
    """Whether either the distribution or the gap list was capped."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.name,
            "label": self.label,
            "present": self.present,
            "values": [item.to_dict() for item in self.values],
            "gaps": list(self.gaps),
            "missing": self.missing,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """What one build contains, measured against the catalog it was drawn from (ADR 0033)."""

    build_hash: str
    episode_count: int
    catalog_size: int
    axes: tuple[CoverageAxis, ...]
    vocabulary_total: int
    """Entries in the task vocabulary, whether or not an episode exists for them."""

    vocabulary_missing: int
    """Entries with no episode in this build."""

    @property
    def coverage_ratio(self) -> float:
        """Share of the catalog's episode population this build accounts for."""
        if not self.catalog_size:
            return 0.0
        return self.episode_count / self.catalog_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": "build-coverage-v1",
            "build_hash": self.build_hash,
            "episode_count": self.episode_count,
            "catalog_size": self.catalog_size,
            "coverage_ratio": self.coverage_ratio,
            "axes": [axis.to_dict() for axis in self.axes],
            "vocabulary_total": self.vocabulary_total,
            "vocabulary_missing": self.vocabulary_missing,
        }

    def axis(self, name: str) -> CoverageAxis | None:
        for item in self.axes:
            if item.name == name:
                return item
        return None


def coverage_report(
    build_hash: str,
    inputs: Mapping[str, Any],
    *,
    value_limit: int = VALUE_LIMIT,
    gap_limit: int = GAP_LIMIT,
) -> CoverageReport:
    """Fold one `build_coverage_inputs` result into the report a surface renders."""
    episode_count = _count(inputs.get("episode_count"))
    catalog_size = _count(inputs.get("catalog_size"))
    rows = _rows(inputs.get("values"))
    vocabulary = inputs.get("vocabulary")
    vocabulary = vocabulary if isinstance(vocabulary, Mapping) else {}
    gaps_by_axis = _vocabulary_gaps(vocabulary, gap_limit)

    axes: list[CoverageAxis] = []
    for name, label in AXES:
        axis_rows = [row for row in rows if row.get("axis") == name]
        values = tuple(
            CoverageValue(
                value=str(row.get("value") or ""),
                count=_count(row.get("build_count")),
                share=_share(_count(row.get("build_count")), episode_count),
            )
            for row in _ordered(axis_rows, value_limit)
        )
        if name == _VOCABULARY_AXIS:
            gaps, missing = gaps_by_axis
            truncated = len(gaps) < missing or len(values) < _distinct(axis_rows, "build_distinct")
        else:
            gaps, missing = _catalog_gaps(axis_rows, gap_limit)
            truncated = len(gaps) < missing or len(values) < _distinct(axis_rows, "build_distinct")
        axes.append(
            CoverageAxis(
                name=name,
                label=label,
                present=_distinct(axis_rows, "build_distinct"),
                values=values,
                gaps=gaps,
                missing=missing,
                truncated=truncated,
            )
        )

    return CoverageReport(
        build_hash=build_hash,
        episode_count=episode_count,
        catalog_size=catalog_size,
        axes=tuple(axes),
        vocabulary_total=_count(vocabulary.get("total")),
        vocabulary_missing=_count(vocabulary.get("missing_total")) or gaps_by_axis[1],
    )


def _ordered(rows: Sequence[Mapping[str, Any]], limit: int) -> list[Mapping[str, Any]]:
    """The build's distribution: present values only, most-common first, ties by value."""
    present = [row for row in rows if _count(row.get("build_count")) > 0]
    present.sort(key=lambda row: (-_count(row.get("build_count")), str(row.get("value") or "")))
    return present[: max(limit, 0)]


def _catalog_gaps(rows: Sequence[Mapping[str, Any]], limit: int) -> tuple[tuple[str, ...], int]:
    """Values the catalog holds and the build does not, and how many there were before the cap."""
    gaps = sorted(
        str(row.get("value") or "")
        for row in rows
        if _count(row.get("build_count")) == 0 and _count(row.get("catalog_count")) > 0
    )
    return tuple(gaps[: max(limit, 0)]), max(_distinct(rows, "gap_total"), len(gaps))


def _vocabulary_gaps(vocabulary: Mapping[str, Any], limit: int) -> tuple[tuple[str, ...], int]:
    """The task axis's gaps: vocabulary entries with no episode in this build."""
    entries = vocabulary.get("gaps")
    labels = [
        str(entry.get("label") or "")
        for entry in (entries if isinstance(entries, Sequence) else [])
        if isinstance(entry, Mapping)
    ]
    labels.sort()
    missing = _count(vocabulary.get("missing_total"))
    return tuple(labels[: max(limit, 0)]), max(missing, len(labels))


def _distinct(rows: Sequence[Mapping[str, Any]], key: str) -> int:
    """One axis's total before the cap, taken from the query's window count."""
    for row in rows:
        value = _count(row.get(key))
        if value:
            return value
    return 0


def _rows(raw: Any) -> list[Mapping[str, Any]]:
    if not isinstance(raw, Sequence):
        return []
    return [row for row in raw if isinstance(row, Mapping)]


def _count(value: Any) -> int:
    """A count, or 0 for anything that is not a finite number (ADR 0023's rule, one layer down)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return 0
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return 0
    return int(number)


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0
