"""Immutable filesystem artifact store keyed by SHA-256."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO


class ArtifactIntegrityError(ValueError):
    """Raised when stored bytes do not match their content address."""


class FileArtifactStore:
    """Store blobs as ``blobs/sha256/<prefix>/<digest>`` with atomic publication."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put_bytes(self, data: bytes) -> str:
        """Atomically store bytes and return their lowercase SHA-256 digest."""
        return self._publish(hashlib.sha256(data).hexdigest(), data)

    def put_file(self, source: Path, *, chunk_bytes: int = 1 << 20) -> str:
        """Content-address a file on disk without loading it into memory.

        Real episodes are files, not dicts: a single LeRobot Parquet shard is hundreds
        of megabytes, so the digest is computed by streaming and the bytes are then
        copied in chunks. The source is not mutated, and a half-written blob never
        becomes visible because publication is the same atomic `os.link` as
        `put_bytes`.
        """
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as handle:
            while chunk := handle.read(chunk_bytes):
                digest.update(chunk)
                size += len(chunk)
        if size == 0:
            raise ValueError(f"refusing to store an empty artifact: {source}")
        destination = self.path_for(digest.hexdigest())
        if destination.exists():
            self._verify(destination, digest.hexdigest())
            return digest.hexdigest()
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as handle:
                while chunk := handle.read(chunk_bytes):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temp_name, destination)
            except FileExistsError:
                self._verify(destination, digest.hexdigest())
        finally:
            os.unlink(temp_name)
        return digest.hexdigest()

    def _publish(self, digest: str, data: bytes) -> str:
        """Shared tail of the in-memory write path."""
        destination = self.path_for(digest)
        if destination.exists():
            self._verify(destination, digest)
            return digest

        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temp_name, destination)
            except FileExistsError:
                self._verify(destination, digest)
        finally:
            Path(temp_name).unlink(missing_ok=True)
        return digest

    def put(self, source: BinaryIO) -> str:
        """Store a binary stream. Vertical-slice payloads are deliberately small."""
        return self.put_bytes(source.read())

    def get_bytes(self, content_hash: str) -> bytes:
        path = self.path_for(content_hash)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != content_hash:
            raise ArtifactIntegrityError(f"artifact checksum mismatch: {content_hash}")
        return data

    def get(self, content_hash: str) -> BinaryIO:
        return self.path_for(content_hash).open("rb")

    def path_for(self, content_hash: str) -> Path:
        if len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash):
            raise ValueError("content_hash must be a lowercase SHA-256 digest")
        return self.root / "blobs" / "sha256" / content_hash[:2] / content_hash

    @staticmethod
    def _verify(path: Path, expected_hash: str) -> None:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected_hash:
            raise ArtifactIntegrityError(f"artifact checksum mismatch: {expected_hash}")
