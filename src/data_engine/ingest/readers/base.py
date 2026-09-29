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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


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
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    std: float | None = None
    shape: tuple[int, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dtype": self.dtype,
            "count": self.count,
            "min": self.min,
            "max": self.max,
            "mean": self.mean,
            "std": self.std,
            "shape": list(self.shape) if self.shape is not None else None,
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
            "duration_seconds": self.duration_seconds,
            "fps": self.fps,
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
