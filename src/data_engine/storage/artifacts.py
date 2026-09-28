"""Content-addressed artifact store boundary; filesystem implementation is phase 05."""

from pathlib import Path
from typing import BinaryIO, Protocol


class ArtifactStore(Protocol):
    def put(self, source: BinaryIO) -> str: ...

    def get(self, content_hash: str) -> BinaryIO: ...

    def path_for(self, content_hash: str) -> Path: ...
