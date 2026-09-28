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
