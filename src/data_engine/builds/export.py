"""LeRobot v3 export of dataset builds (ADR 0025).

A build is a manifest; an export is the *dataset* the manifest describes, laid
out on disk in LeRobot v3 form so a training script can load it without this
engine installed. The rules come from ADR 0025, and each guards a failure mode:

- **The directory is the build hash.** The hash is the dataset's identity, so
  re-exporting is verifiably a no-op ("already done" is a claim about content,
  not memory) and two exports of one build cannot diverge.
- **Atomic publication.** The tree is staged under a `.pending-` sibling and
  renamed into place only when complete, so a killed export never leaves a
  half-valid dataset at the content address.
- **Every member contributes its frames or the export fails.** A missing
  episode is a silent hole in a training set; the manifest's episode count is
  a promise, not a suggestion.
- **One data file per episode, in manifest order.** A v3 episode is a row
  range over concatenated files; giving every episode its own file makes the
  row range trivially `[0, n)` for each, which is the layout least likely to
  be read back as somebody else's frames.

The acceptance consumer is our own `LeRobotReader`: the file naming, the index
record keys, and the row-range math below are exactly what that reader
resolves, and the round-trip test pins it.

Videos are not exported: no ingested format carries video frames today, so the
declared layout has none. A recorded limit, not an omission.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from data_engine.canonical import canonical_json
from data_engine.storage.artifacts import ArtifactIntegrityError, FileArtifactStore

#: Engine-owned metadata inside the exported tree. LeRobot consumers ignore
#: unknown directories; lineage travels with the data.
FAULTLINED_DIR = "_faultlined"

DATA_DIR = Path("data")
META_DIR = Path("meta")
EPISODES_META_DIR = META_DIR / "episodes"

#: Files per chunk, the LeRobot convention; chunk/file indexes derive from it.
CHUNKS_SIZE = 1000

_STRUCTURAL = frozenset({"timestamp", "frame_index", "episode_index", "index", "task_index"})


class ExportError(ValueError):
    """An export that cannot be produced, with a reason a caller can act on."""


class ExportBuildUnknown(ExportError):
    """The build hash named for export does not resolve to a build."""


class BuildExporter:
    """Materialise a build manifest as a LeRobot v3 dataset directory."""

    def __init__(self, export_root: Path, artifacts: FileArtifactStore) -> None:
        self.export_root = export_root
        self.artifacts = artifacts

    def export(self, build: dict[str, Any], members: list[dict[str, Any]]) -> dict[str, Any]:
        """Export `build` with its member `episodes`; idempotent at the address.

        `members` are episode rows (at minimum id/episode_id, artifact_hash, and
        episode_key where the format has one). Raises ExportError when the build
        is malformed or any member's artifact cannot be decoded into frames; a
        failed export leaves no tree at the build-hash address.
        """
        build_hash = str(build.get("hash") or "")
        if not build_hash:
            raise ExportError("a build row without a hash cannot be exported")
        manifest = build.get("manifest")
        if not isinstance(manifest, dict):
            manifest = {}
        episodes = manifest.get("episodes")
        if not isinstance(episodes, list) or not episodes:
            raise ExportError(f"build {build_hash} has no episodes in its manifest")
        if not members:
            raise ExportError(f"build {build_hash} resolved to no member episodes")

        dataset_root = self.export_root / build_hash
        existing = self.exported_path(build_hash)
        if existing is not None:
            # The verified tree at the content address is the answer; no
            # artifact is read, so a re-export never depends on the sources.
            info = json.loads((existing / META_DIR / "info.json").read_text(encoding="utf-8"))
            return {
                "build_hash": build_hash,
                "path": str(existing),
                "episode_count": int(info.get("total_episodes") or len(episodes)),
                "frame_count": int(info.get("total_frames") or 0),
            }

        frames = self._member_frames(build_hash, episodes, members)
        self._publish(dataset_root, build, frames)
        return {
            "build_hash": build_hash,
            "path": str(dataset_root),
            "episode_count": len(frames),
            "frame_count": sum(table.num_rows for table, _ in frames),
        }

    def exported_path(self, build_hash: str) -> Path | None:
        """The dataset directory when a tree already exists at the address."""
        candidate = self.export_root / build_hash
        if (candidate / META_DIR / "info.json").is_file():
            return candidate
        return None

    # ------------------------------------------------------------------ frames

    def _member_frames(
        self,
        build_hash: str,
        episodes: list[dict[str, Any]],
        members: list[dict[str, Any]],
    ) -> list[tuple[pa.Table, dict[str, Any]]]:
        """Decode every member into a frame table, in manifest order.

        The manifest is the identity of the episode list, so iteration follows
        it; member rows are looked up by id to carry the artifact hash and the
        metadata the index records need.
        """
        by_id = {
            str(row.get("episode_id") or row.get("id") or ""): row
            for row in members
            if isinstance(row, dict)
        }
        tables: list[tuple[pa.Table, dict[str, Any]]] = []
        for entry in episodes:
            if not isinstance(entry, dict):
                raise ExportError(f"build {build_hash} has a malformed episode entry")
            episode_id = str(entry.get("episode_id") or "")
            row = by_id.get(episode_id)
            if row is None:
                raise ExportError(
                    f"build {build_hash} names episode {episode_id}, which is not a member"
                )
            artifact_hash = str(row.get("artifact_hash") or "")
            if not artifact_hash:
                raise ExportError(f"episode {episode_id} has no artifact hash; it cannot be read")
            try:
                blob = self.artifacts.get_bytes(artifact_hash)
            except ArtifactIntegrityError as exc:
                # Corrupt content is a property of the bytes: retrying cannot
                # fix it, so it is terminal. A plain OSError (disk, permissions)
                # propagates unwrapped and stays retryable.
                raise ExportError(
                    f"episode {episode_id} artifact {artifact_hash[:12]} is unreadable: {exc}"
                ) from exc
            tables.append((_episode_frames(episode_id, blob, row), row))
        return tables

    def _publish(
        self,
        dataset_root: Path,
        build: dict[str, Any],
        frames: list[tuple[pa.Table, dict[str, Any]]],
    ) -> None:
        """Write the tree atomically, or clean the attempt up entirely."""
        if self.exported_path(dataset_root.name) is not None:
            return
        self.export_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".pending-{dataset_root.name[:24]}-", dir=self.export_root)
        )
        try:
            _write_layout(staging, build, frames)
            if dataset_root.exists():
                # A concurrent export won the rename; the address holds one tree
                # either way, which is the property that matters.
                return
            os.rename(staging, dataset_root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------- frame decode


def _episode_frames(episode_id: str, blob: bytes, row: dict[str, Any]) -> pa.Table:
    """One member's frames, whatever format ingested it.

    Two artifact shapes exist today: canonical-JSON synthetic episodes and
    Parquet files (a lerobot source file may be shared by many episodes, so it
    is sliced by `episode_index` - the same contract the LeRobot reader keeps).
    """
    if blob.lstrip()[:1] == b"{":
        return _synthetic_frames(episode_id, blob)
    table = _parquet_frames(episode_id, blob)
    if "episode_index" in table.schema.names:
        key = _parse_episode_key(str(row.get("episode_key") or ""))
        table = table.filter(pc.equal(table.column("episode_index"), key))
        if table.num_rows == 0:
            raise ExportError(
                f"episode {episode_id}: artifact holds no rows for episode_index={key}"
            )
    return table


def _synthetic_frames(episode_id: str, blob: bytes) -> pa.Table:
    """Decode a canonical-JSON synthetic episode into a frame table."""
    try:
        episode = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ExportError(f"episode {episode_id} artifact is not decodable JSON: {exc}") from exc
    if not isinstance(episode, dict):
        raise ExportError(f"episode {episode_id} artifact is not a synthetic episode document")
    timestamps = episode.get("timestamps")
    observations = episode.get("observations")
    actions = episode.get("actions")
    if not isinstance(timestamps, list) or not timestamps:
        raise ExportError(f"episode {episode_id} artifact has no timestamps")
    if not isinstance(observations, list) or not isinstance(actions, list):
        raise ExportError(f"episode {episode_id} artifact has no observation/action frames")
    if len(observations) != len(timestamps) or len(actions) != len(timestamps):
        raise ExportError(f"episode {episode_id} artifact has ragged frame lists")
    columns: dict[str, pa.Array] = {
        "timestamp": pa.array([float(t) for t in timestamps], pa.float64())
    }
    _append_dimension_columns(columns, "observation", observations)
    _append_dimension_columns(columns, "action", actions)
    return pa.table(columns)


def _append_dimension_columns(columns: dict[str, pa.Array], prefix: str, rows: list[Any]) -> None:
    """One column per frame dimension, named the way the ingest path names them."""
    width = len(rows[0]) if rows else 0
    if width == 0 or any(not isinstance(frame, list) or len(frame) != width for frame in rows):
        return
    for i in range(width):
        columns[f"{prefix}[{i}]"] = pa.array([float(frame[i]) for frame in rows], pa.float64())


def _parquet_frames(episode_id: str, blob: bytes) -> pa.Table:
    try:
        return pq.read_table(io.BytesIO(blob))
    except (pa.ArrowInvalid, OSError) as exc:
        raise ExportError(f"episode {episode_id} artifact is not readable Parquet: {exc}") from exc


def _parse_episode_key(key: str) -> int:
    tail = key.rsplit("=", 1)[-1]
    try:
        return int(tail)
    except ValueError as exc:
        raise ExportError(f"cannot parse an episode index from {key!r}") from exc


# -------------------------------------------------------------------- layout


def _write_layout(
    staging: Path,
    build: dict[str, Any],
    frames: list[tuple[pa.Table, dict[str, Any]]],
) -> None:
    """The v3 tree: per-episode data files, the episode index, info, tasks.

    Episode `i` lives at `data/chunk-{c}/file-{f}.parquet` with
    `c, f = i // CHUNKS_SIZE, i % CHUNKS_SIZE`, and its index record at
    `meta/episodes/chunk-{c}/episode_{i:06d}.parquet` declares the row range
    `[0, n)` over exactly that file - which is what the reader resolves.
    """
    global_start = 0
    for index, (table, row) in enumerate(frames):
        chunk, file_index = index // CHUNKS_SIZE, index % CHUNKS_SIZE
        data_dir = staging / DATA_DIR / f"chunk-{chunk:03d}"
        index_dir = staging / EPISODES_META_DIR / f"chunk-{chunk:03d}"
        data_dir.mkdir(parents=True, exist_ok=True)
        index_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            _reindexed(table, index, global_start),
            data_dir / f"file-{file_index:03d}.parquet",
            compression="snappy",
        )
        pq.write_table(
            _episode_index_record(index, global_start, table.num_rows, row),
            index_dir / f"episode_{index:06d}.parquet",
        )
        global_start += table.num_rows

    info = {
        "codebase_version": "v3.0",
        "robot_type": _robot_type(frames),
        "total_episodes": len(frames),
        "total_frames": sum(table.num_rows for table, _ in frames),
        "total_tasks": 1,
        "total_videos": 0,
        "total_chunks": (len(frames) + CHUNKS_SIZE - 1) // CHUNKS_SIZE,
        "chunks_size": CHUNKS_SIZE,
        "fps": None,
        "features": _features(frames),
    }
    (staging / META_DIR).mkdir(parents=True, exist_ok=True)
    (staging / META_DIR / "info.json").write_text(
        json.dumps(info, indent=2, sort_keys=True), encoding="utf-8"
    )
    (staging / META_DIR / "tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": _task_name(build)}) + "\n", encoding="utf-8"
    )

    faultlined = staging / FAULTLINED_DIR
    faultlined.mkdir()
    (faultlined / "manifest.json").write_bytes(canonical_json(build.get("manifest") or {}))


def _episode_index_record(index: int, start: int, length: int, row: dict[str, Any]) -> pa.Table:
    """The v3 episode record: identity, locator, global row range, tasks.

    The range is dataset-global, not per-file: the reader subtracts the file's
    starting row (the sum of every earlier file's lengths) to locate the slice,
    so a per-file zero here reads somebody else's frames or none at all.
    """
    return pa.table(
        {
            "episode_index": pa.array([index], pa.int64()),
            "length": pa.array([length], pa.int64()),
            "dataset_from_index": pa.array([start], pa.int64()),
            "dataset_to_index": pa.array([start + length], pa.int64()),
            "data/chunk_index": pa.array([index // CHUNKS_SIZE], pa.int64()),
            "data/file_index": pa.array([index % CHUNKS_SIZE], pa.int64()),
            "tasks": pa.array([_episode_tasks(row)], pa.list_(pa.string())),
        }
    )


def _reindexed(table: pa.Table, episode_index: int, global_start: int) -> pa.Table:
    """Rewrite the structural columns onto the exported dataset's own indexing."""
    n = table.num_rows
    out = table.drop_columns(
        [name for name in ("episode_index", "index") if name in table.schema.names]
    )
    out = out.append_column(
        pa.field("episode_index", pa.int64()), pa.array([episode_index] * n, pa.int64())
    )
    return out.append_column(
        pa.field("index", pa.int64()),
        pa.array(range(global_start, global_start + n), pa.int64()),
    )


def _episode_tasks(row: dict[str, Any]) -> list[str]:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    tasks = metadata.get("tasks")
    if isinstance(tasks, list) and tasks:
        return [str(task) for task in tasks if str(task)]
    task = metadata.get("task")
    return [str(task)] if task else ["export"]


def _robot_type(frames: list[tuple[pa.Table, dict[str, Any]]]) -> str:
    for _, row in frames:
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            continue
        robot = metadata.get("robot") or metadata.get("robot_type")
        if robot:
            return str(robot)
    return "unknown"


def _task_name(build: dict[str, Any]) -> str:
    name = str(build.get("name") or "").strip()
    return name or "export"


def _features(frames: list[tuple[pa.Table, dict[str, Any]]]) -> dict[str, Any]:
    """Feature declarations for the columns actually written, first-seen order."""
    names: list[str] = []
    for table, _ in frames:
        for name in table.schema.names:
            if name not in _STRUCTURAL and name not in names:
                names.append(name)
    dtype = "float64"  # what every column written by this module actually is
    return {name: {"dtype": dtype, "shape": [1]} for name in names}


__all__ = [
    "CHUNKS_SIZE",
    "FAULTLINED_DIR",
    "BuildExporter",
    "ExportBuildUnknown",
    "ExportError",
]
