"""Format dispatch by sniffing, so a new format is a registration, not an edit."""

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
    """Sniff, then read."""
    reader = reader_for(path)
    if reader is None:
        tried = ", ".join(r.format for r in READERS) or "none"
        raise ReaderError(f"no reader recognises {path} (tried: {tried})")
    return reader.read(path, episode_key=episode_key)


__all__ = ["READERS", "read_episode", "reader_for"]
