"""Format-agnostic episode reader boundary."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from data_engine.analysis.quality import EpisodeQuality


def finite_or_none(value: Any) -> Any:
    """A real number passes through when it is finite; anything else is left alone."""
    if isinstance(value, list | tuple):
        return [finite_or_none(item) for item in value]
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return value
    return value if math.isfinite(value) else None


class ReaderError(ValueError):
    """Source data does not parse as the format it was claimed to be."""


@dataclass(frozen=True, slots=True)
class ChannelStats:
    """Published or computed summary of one feature column."""

    name: str
    dtype: str
    count: int
    min: float | Sequence[float] | None = None
    max: float | Sequence[float] | None = None
    mean: float | Sequence[float] | None = None
    std: float | Sequence[float] | None = None
    shape: tuple[int, ...] | None = None
    nonfinite: int = 0
    """Samples the reader refused to turn into a statistic. """

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
    """Engine-normalized format name, e.g. """

    format_version: str
    """The format's own version string, verbatim from the artifact (`v2.1`, `v3.0`)."""

    episode_key: str
    """Stable identifier within its dataset, e.g. """

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
        """Whether `path` is this format."""
        ...

    def read(self, path: Path, *, episode_key: str | None = None) -> EpisodeExtraction:
        """Describe one episode, or raise `ReaderError` if the path does not parse."""
        ...
