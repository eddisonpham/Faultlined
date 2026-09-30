"""Format dispatch by sniffing, so a new format is a registration, not an edit.

`api.md` §reader protocol promises that "adding a format (ROS 2 bag later) means adding
one reader + tests — no core changes". This module is the single place where that promise
is kept, and it is deliberately boring: ordered readers, first sniff wins, no plugin
discovery and no entry-point machinery for a platform that has two formats.
"""

from __future__ import annotations

from pathlib import Path

from data_engine.ingest.readers.base import EpisodeExtraction, EpisodeReader, ReaderError
from data_engine.ingest.readers.lerobot import LeRobotReader
from data_engine.ingest.readers.mcap_reader import McapReader

READERS: tuple[EpisodeReader, ...] = (McapReader(), LeRobotReader())


def reader_for(path: Path) -> EpisodeReader | None:
    """The reader that claims `path`, or None when nothing recognises it."""
    for reader in READERS:
        if reader.sniff(path):
            return reader
    return None


def read_episode(path: Path, *, episode_key: str | None = None) -> EpisodeExtraction:
    """Sniff, then read. Raises `ReaderError` naming the formats that were tried."""
    reader = reader_for(path)
    if reader is None:
        tried = ", ".join(r.format for r in READERS) or "none"
        raise ReaderError(f"no reader recognises {path} (tried: {tried})")
    return reader.read(path, episode_key=episode_key)


__all__ = ["READERS", "read_episode", "reader_for"]
