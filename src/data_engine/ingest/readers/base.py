"""Format-agnostic episode reader boundary.

A reader's job is narrow and deliberately so: turn a path on disk into a small
queryable *description* of one episode, without ever holding the episode's frames in
memory. The frames stay in the artifact store, addressed by the hash of the bytes that
produced them; the catalog keeps the summary that makes an episode findable
(`architecture/storage.md` §1, `episode_metadata`).

That split is the reason the extraction is a description and not the data. A single
LeRobot episode is a few thousand rows and an MCAP file is a sensor log; serialising
either into the catalog would make ingest O(dataset) and defeat content addressing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from data_engine.analysis.quality import EpisodeQuality


def finite_or_none(value: Any) -> Any:
    """A real number passes through when it is finite; anything else is left alone.

    Postgres `jsonb` cannot store `NaN` or `Infinity` - Python's `json` module
    has always written them and the database rejects them - so one infinite
    sample made `register_episode`'s metadata insert raise
    `InvalidTextRepresentation`, which the worker classified retryable and retried
    three times over the same bytes (EXP-0014 D2). A statistic that is not a
    number is not a measurement, so it is written as `None` (absent) rather than
    taking the rest of the episode's record down with it.

    A vector is sanitized element by element: the LeRobot reader carries a
    multi-dimensional feature's published `min`/`max` as a vector, and an infinite
    element inside one is exactly the defect this guards, so leaving the vector
    untouched would only move the problem. A value of any other shape is returned
    unchanged - it is not a statistic this function has an opinion about.
    """
    if isinstance(value, list | tuple):
        return [finite_or_none(item) for item in value]
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return value
    return value if math.isfinite(value) else None


class ReaderError(ValueError):
    """Source data does not parse as the format it was claimed to be.

    Terminal by construction: retrying the same bytes cannot change the answer, so the
    worker maps this to a non-retryable `INGEST_*` reason code rather than burning the
    job's attempt budget (`architecture/failure-handling.md` F1). The synthetic JSON
    path raises it too - it is the smallest reader the engine has.
    """


@dataclass(frozen=True, slots=True)
class ChannelStats:
    """Published or computed summary of one feature column."""

    name: str
    dtype: str
    count: int
    # A published multi-dimensional feature carries its per-dimension statistics as
    # a vector, so these are a number *or* a number per dimension - not the
    # `float | None` they were annotated with while the reader already stored lists.
    min: float | Sequence[float] | None = None
    max: float | Sequence[float] | None = None
    mean: float | Sequence[float] | None = None
    std: float | Sequence[float] | None = None
    shape: tuple[int, ...] | None = None
    nonfinite: int = 0
    """Samples the reader refused to turn into a statistic. Non-zero means the
    camera/encoder/decoder emitted a value that is not a number, and the fields
    above describe only the finite samples; it is *not* a silent drop."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dtype": self.dtype,
            "count": self.count,
            "min": finite_or_none(self.min),
            "max": finite_or_none(self.max),
            "mean": finite_or_none(self.mean),
            "std": finite_or_none(self.std),
            "shape": list(self.shape) if self.shape is not None else None,
            "nonfinite": self.nonfinite,
        }


@dataclass(frozen=True, slots=True)
class EpisodeExtraction:
    """One episode, described: identity, provenance, and per-channel summary."""

    format: str
    """Engine-normalized format name, e.g. `lerobot-v3`."""

    format_version: str
    """The format's own version string, verbatim from the artifact (`v2.1`, `v3.0`)."""

    episode_key: str
    """Stable identifier within its dataset, e.g. `episode_index=3`."""

    source_path: Path
    """The file the episode's rows were actually read from."""

    robot_type: str
    task: str
    tasks: tuple[str, ...]
    frame_count: int
    duration_seconds: float
    fps: float | None = None
    channels: tuple[ChannelStats, ...] = ()
    quality: EpisodeQuality | None = None
    """Motion-quality summary computed while the rows were in memory (ADR 0018)."""

    dataset: dict[str, Any] = field(default_factory=dict)
    """Dataset-level facts (total episodes, feature names, robot type) for context."""

    def metadata(self) -> dict[str, Any]:
        """The `episode_metadata` record this extraction maps onto."""
        return {
            "format": self.format,
            "format_version": self.format_version,
            "episode_key": self.episode_key,
            "robot": self.robot_type,
            "task": self.task,
            "tasks": list(self.tasks),
            "frame_count": self.frame_count,
            "duration_seconds": finite_or_none(self.duration_seconds),
            "fps": finite_or_none(self.fps),
            "channel_stats": {channel.name: channel.to_dict() for channel in self.channels},
        }


class EpisodeReader(Protocol):
    format: str

    def sniff(self, path: Path) -> bool:
        """Whether `path` is this format. Must not raise, and must not read the data."""
        ...

    def read(self, path: Path, *, episode_key: str | None = None) -> EpisodeExtraction:
        """Describe one episode, or raise `ReaderError` if the path does not parse."""
        ...
