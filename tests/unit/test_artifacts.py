import hashlib
from pathlib import Path

import pytest

from data_engine.storage.artifacts import ArtifactIntegrityError, FileArtifactStore


@pytest.mark.unit
def test_artifact_store_put_deduplicates_and_reads_verified_bytes(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    payload = b'{"episode":1}'

    first = store.put_bytes(payload)
    second = store.put_bytes(payload)

    assert first == second == hashlib.sha256(payload).hexdigest()
    assert store.get_bytes(first) == payload
    assert store.path_for(first).is_file()


@pytest.mark.unit
def test_artifact_store_rejects_invalid_digest(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        store.path_for("../bad")


@pytest.mark.unit
def test_artifact_store_detects_corrupt_existing_blob(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path)
    digest = hashlib.sha256(b"expected").hexdigest()
    path = store.path_for(digest)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrupt")

    with pytest.raises(ArtifactIntegrityError, match="checksum mismatch"):
        store.put_bytes(b"expected")


@pytest.mark.unit
def test_put_file_addresses_bytes_without_reading_them_at_once(tmp_path: Path) -> None:
    """Real episodes are files; a 1 MiB chunk keeps memory flat for a large shard."""
    source = tmp_path / "shard.parquet"
    payload = b"episode-bytes" * 10_000
    source.write_bytes(payload)
    store = FileArtifactStore(tmp_path / "store")

    digest = store.put_file(source, chunk_bytes=1024)

    assert digest == hashlib.sha256(payload).hexdigest()
    assert store.get_bytes(digest) == payload
    assert source.read_bytes() == payload, "the source must not be consumed"


@pytest.mark.unit
def test_put_file_is_idempotent_for_identical_content(tmp_path: Path) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "nested" / "b.bin"
    second.parent.mkdir()
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    store = FileArtifactStore(tmp_path / "store")

    assert store.put_file(first) == store.put_file(second)
    assert [p.name for p in (tmp_path / "store").rglob("*") if p.is_file()] == [
        hashlib.sha256(b"same").hexdigest()
    ], "identical content must be stored once"


@pytest.mark.unit
def test_put_file_refuses_an_empty_artifact(tmp_path: Path) -> None:
    source = tmp_path / "empty.bin"
    source.touch()
    with pytest.raises(ValueError, match="empty artifact"):
        FileArtifactStore(tmp_path / "store").put_file(source)


@pytest.mark.unit
def test_put_file_detects_a_corrupt_existing_blob(tmp_path: Path) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"expected")
    store = FileArtifactStore(tmp_path / "store")
    digest = hashlib.sha256(b"expected").hexdigest()
    store.path_for(digest).parent.mkdir(parents=True)
    store.path_for(digest).write_bytes(b"corrupt")

    with pytest.raises(ArtifactIntegrityError, match="checksum mismatch"):
        store.put_file(source)
