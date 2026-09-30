"""Unit tests for the LeRobot v3 build exporter (ADR 0025).

The acceptance consumer is our own `LeRobotReader`: several tests read the
exported tree back through it, because an export our own reader rejects is a
defective export by definition. No database is needed - the exporter takes
plain dicts and a real `FileArtifactStore` over `tmp_path`.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from data_engine.builds.export import FAULTLINED_DIR, BuildExporter, ExportError
from data_engine.canonical import canonical_json
from data_engine.ingest.readers.lerobot import LeRobotReader
from data_engine.storage.artifacts import FileArtifactStore


def _synthetic_blob(
    *, frames: int = 3, obs_dims: int = 2, act_dims: int = 1, task: str = "pick"
) -> bytes:
    return canonical_json(
        {
            "task": task,
            "robot": "so101",
            "timestamps": [round(0.1 * i, 3) for i in range(frames)],
            "observations": [[round(0.1 * i, 3)] * obs_dims for i in range(frames)],
            "actions": [[round(1.0 + 0.1 * i, 3)] * act_dims for i in range(frames)],
        }
    )


def _member(
    episode_id: str,
    blob_hash: str,
    *,
    episode_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "episode_id": episode_id,
        "id": episode_id,
        "artifact_hash": blob_hash,
        "source_hash": blob_hash,
        "episode_key": episode_key,
        "metadata": metadata or {"robot": "so101", "task": "pick", "frame_count": 3},
    }


def _manifest_entry(episode_id: str, blob_hash: str) -> dict[str, Any]:
    return {
        "episode_id": episode_id,
        "source_hash": blob_hash,
        "artifact_hash": blob_hash,
        "format": "synthetic-json",
    }


def _build(
    build_hash: str, episodes: list[dict[str, Any]], *, name: str = "pick-set"
) -> dict[str, Any]:
    return {
        "hash": build_hash,
        "name": name,
        "episode_count": len(episodes),
        "manifest": {
            "kind": "dataset-build",
            "version": 1,
            "name": name,
            "code_commit": "abc123",
            "episode_count": len(episodes),
            "episodes": episodes,
        },
    }


def _exporter(tmp_path: Path) -> tuple[BuildExporter, FileArtifactStore]:
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    return BuildExporter(tmp_path / "exports", artifacts), artifacts


def test_export_writes_a_lerobot_v3_tree(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    h1 = artifacts.put_bytes(_synthetic_blob())
    h2 = artifacts.put_bytes(_synthetic_blob(frames=4))
    build = _build("bld_one", [_manifest_entry("ep-1", h1), _manifest_entry("ep-2", h2)])

    result = exporter.export(build, [_member("ep-1", h1), _member("ep-2", h2)])

    root = tmp_path / "exports" / "bld_one"
    assert result["path"] == str(root)
    assert result["episode_count"] == 2
    assert result["frame_count"] == 7  # 3 + 4
    info = json.loads((root / "meta" / "info.json").read_text(encoding="utf-8"))
    assert info["codebase_version"] == "v3.0"
    assert info["total_episodes"] == 2
    assert info["total_frames"] == 7
    assert info["total_videos"] == 0
    assert (root / "data" / "chunk-000" / "file-000.parquet").is_file()
    assert (root / "data" / "chunk-000" / "file-001.parquet").is_file()
    manifest = json.loads((root / FAULTLINED_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == build["manifest"]


def test_our_own_reader_reads_the_export_back(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    h1 = artifacts.put_bytes(_synthetic_blob(frames=3))
    h2 = artifacts.put_bytes(_synthetic_blob(frames=5, task="place"))
    build = _build("bld_rt", [_manifest_entry("ep-1", h1), _manifest_entry("ep-2", h2)])
    exporter.export(
        build,
        [
            _member("ep-1", h1, metadata={"robot": "so101", "task": "pick", "frame_count": 3}),
            _member("ep-2", h2, metadata={"robot": "so101", "task": "place", "frame_count": 5}),
        ],
    )

    reader = LeRobotReader()
    root = tmp_path / "exports" / "bld_rt"
    assert reader.sniff(root)
    first = reader.read(root, episode_key="episode_index=0")
    second = reader.read(root, episode_key="episode_index=1")

    assert first.format == "lerobot-v3"
    assert first.frame_count == 3
    assert first.task == "pick"
    assert first.robot_type == "so101"
    assert second.frame_count == 5
    # The worst failure mode available here is frames from the wrong episode.
    assert second.task == "place"
    assert first.source_path.name == "file-000.parquet"
    assert second.source_path.name == "file-001.parquet"


def test_export_is_idempotent_at_the_content_address(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    h1 = artifacts.put_bytes(_synthetic_blob())
    build = _build("bld_idem", [_manifest_entry("ep-1", h1)])
    members = [_member("ep-1", h1)]

    first = exporter.export(build, members)
    tree = tmp_path / "exports" / "bld_idem"
    before = sorted((str(p.relative_to(tree)), p.stat().st_mtime_ns) for p in tree.rglob("*"))
    second = exporter.export(build, members)

    assert second["path"] == first["path"]
    after = sorted((str(p.relative_to(tree)), p.stat().st_mtime_ns) for p in tree.rglob("*"))
    assert after == before  # the existing tree was not rewritten
    assert list((tmp_path / "exports").glob(".pending-*")) == []


def test_a_failed_export_leaves_no_tree_at_the_address(tmp_path: Path) -> None:
    exporter, _artifacts = _exporter(tmp_path)
    build = _build("bld_fail", [_manifest_entry("ep-missing", "f" * 64)])

    try:
        exporter.export(build, [])
    except ExportError:
        pass
    else:  # pragma: no cover - the assertion is the absence of a tree
        raise AssertionError("export of an empty member list must raise")

    assert not (tmp_path / "exports" / "bld_fail").exists()
    assert list((tmp_path / "exports").glob(".pending-*")) == []


def test_manifest_order_resolves_members_not_list_order(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    h_a = artifacts.put_bytes(_synthetic_blob(frames=3, task="pick"))
    h_b = artifacts.put_bytes(_synthetic_blob(frames=2, task="place"))
    # The manifest's order is the identity: b first.
    build = _build("bld_ord", [_manifest_entry("ep-b", h_b), _manifest_entry("ep-a", h_a)])
    exporter.export(
        build,
        [
            _member("ep-a", h_a, metadata={"robot": "so101", "task": "pick", "frame_count": 3}),
            _member("ep-b", h_b, metadata={"robot": "so101", "task": "place", "frame_count": 2}),
        ],
    )

    read_back = LeRobotReader().read(
        tmp_path / "exports" / "bld_ord", episode_key="episode_index=0"
    )
    assert read_back.frame_count == 2
    assert read_back.task == "place"


def test_a_member_named_by_the_manifest_but_absent_from_the_rows_fails(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    h1 = artifacts.put_bytes(_synthetic_blob())
    build = _build("bld_hole", [_manifest_entry("ep-1", h1), _manifest_entry("ep-ghost", h1)])

    try:
        exporter.export(build, [_member("ep-1", h1)])
    except ExportError as exc:
        assert "ep-ghost" in str(exc)
    else:
        raise AssertionError("a manifest naming a non-member must fail, not skip it")


def test_a_shared_lerobot_source_file_is_sliced_by_episode_index(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    shared = pa.table(
        {
            "timestamp": pa.array([0.0, 0.1, 0.2, 0.3], pa.float64()),
            "observation[0]": pa.array([1.0, 2.0, 3.0, 4.0], pa.float64()),
            "episode_index": pa.array([0, 0, 1, 1], pa.int64()),
        }
    )
    buffer = io.BytesIO()
    pq.write_table(shared, buffer)
    digest = artifacts.put_bytes(buffer.getvalue())
    build = _build(
        "bld_slice",
        [_manifest_entry("ep-a", digest), _manifest_entry("ep-b", digest)],
    )
    result = exporter.export(
        build,
        [
            _member("ep-a", digest, episode_key="episode_index=0"),
            _member("ep-b", digest, episode_key="episode_index=1"),
        ],
    )
    assert result["frame_count"] == 4  # 2 + 2, each its own file

    file0 = pq.read_table(
        tmp_path / "exports" / "bld_slice" / "data" / "chunk-000" / "file-000.parquet"
    )
    file1 = pq.read_table(
        tmp_path / "exports" / "bld_slice" / "data" / "chunk-000" / "file-001.parquet"
    )
    assert file0.column("episode_index").to_pylist() == [0, 0]
    assert file1.column("observation[0]").to_pylist() == [3.0, 4.0]


def test_synthetic_frames_get_dimension_columns_and_a_timestamp(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    h1 = artifacts.put_bytes(_synthetic_blob(frames=2, obs_dims=3, act_dims=2))
    build = _build("bld_cols", [_manifest_entry("ep-1", h1)])
    exporter.export(build, [_member("ep-1", h1)])

    table = pq.read_table(
        tmp_path / "exports" / "bld_cols" / "data" / "chunk-000" / "file-000.parquet"
    )
    assert table.schema.names == [
        "timestamp",
        "observation[0]",
        "observation[1]",
        "observation[2]",
        "action[0]",
        "action[1]",
        "episode_index",
        "index",
    ]
    info = json.loads(
        (tmp_path / "exports" / "bld_cols" / "meta" / "info.json").read_text(encoding="utf-8")
    )
    assert set(info["features"]) == {
        "observation[0]",
        "observation[1]",
        "observation[2]",
        "action[0]",
        "action[1]",
    }


def test_undecodable_artifact_bytes_fail_the_export(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    garbage = artifacts.put_bytes(b"definitely neither json nor parquet")
    build = _build("bld_garbage", [_manifest_entry("ep-1", garbage)])

    try:
        exporter.export(build, [_member("ep-1", garbage)])
    except ExportError as exc:
        assert "not readable Parquet" in str(exc)
    else:
        raise AssertionError("undecodable artifact must fail the export")


def test_a_ragged_synthetic_artifact_fails_rather_than_exporting_a_lie(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    ragged = canonical_json(
        {
            "task": "pick",
            "robot": "so101",
            "timestamps": [0.0, 0.1, 0.2],
            "observations": [[0.1], [0.2]],  # one frame short
            "actions": [[1.0], [1.1], [1.2]],
        }
    )
    digest = artifacts.put_bytes(ragged)
    build = _build("bld_ragged", [_manifest_entry("ep-1", digest)])

    try:
        exporter.export(build, [_member("ep-1", digest)])
    except ExportError as exc:
        assert "ragged" in str(exc)
    else:
        raise AssertionError("a ragged artifact must not export")


def test_exported_path_reports_an_existing_tree_only(tmp_path: Path) -> None:
    exporter, artifacts = _exporter(tmp_path)
    assert exporter.exported_path("bld_none") is None
    h1 = artifacts.put_bytes(_synthetic_blob())
    build = _build("bld_none", [_manifest_entry("ep-1", h1)])
    exporter.export(build, [_member("ep-1", h1)])
    assert exporter.exported_path("bld_none") == tmp_path / "exports" / "bld_none"
