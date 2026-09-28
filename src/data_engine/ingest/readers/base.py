"""Format-agnostic episode reader boundary."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EpisodeSource:
    path: Path
    format: str
    size_bytes: int


class EpisodeReader(Protocol):
    format: str

    def sniff(self, path: Path) -> bool: ...

    def read(self, path: Path) -> EpisodeSource: ...
