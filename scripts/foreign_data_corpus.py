#!/usr/bin/env python3
"""Generate a corpus of *non-LeRobot* robot data, deterministically.

uv run --with h5py python scripts/foreign_data_corpus.py var/foreign-corpus
uv run --with h5py python scripts/foreign_data_corpus.py var/foreign-corpus --manifest-only
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import struct
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from mcap.writer import CompressionType, Writer

NANOSECONDS_PER_SECOND = 1_000_000_000

JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")


def _json_payload(step: int, joints: int = 6) -> bytes:
    t = step / 50.0
    body: dict[str, Any] = {
        "name": list(JOINTS[:joints]),
        "position": [round(math.sin(0.7 * t + 0.4 * i), 6) for i in range(joints)],
        "velocity": [round(0.7 * math.cos(0.7 * t + 0.4 * i), 6) for i in range(joints)],
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def write_mcap(
    path: Path,
    *,
    seconds: float = 20.0,
    encoding: str = "json",
    channels: dict[str, tuple[float, str]] | None = None,
    messages_for: Any = _json_payload,
    compression: CompressionType = CompressionType.ZSTD,
    task: str = "pick up the red block and place it in the bin",
    robot_type: str = "so101_follower",
) -> dict[str, Any]:
    """One MCAP file with a metadata session record, as a real recorder writes."""
    plan = channels or {"/joint_states": (50.0, "sensor_msgs/msg/JointState")}
    path.parent.mkdir(parents=True, exist_ok=True)
    start = 1_700_000_000 * NANOSECONDS_PER_SECOND
    writer = Writer(str(path), compression=compression)
    ids: dict[str, int] = {}
    counts: dict[str, int] = {}
    try:
        writer.start(profile="", library="faultlined-foreign-corpus")
        writer.add_metadata("session", {"robot_type": robot_type, "task": task})
        for topic, (_hz, schema_name) in plan.items():
            schema_id = writer.register_schema(schema_name, encoding, schema_name.encode("utf-8"))
            ids[topic] = writer.register_channel(topic, encoding, schema_id)
        steps = max(1, int(seconds * 50))
        for step in range(steps):
            stamp = start + int((step / 50.0) * NANOSECONDS_PER_SECOND)
            for topic, (hz, _schema) in plan.items():
                if step % max(1, round(50.0 / hz)):
                    continue
                payload = messages_for(step, topic)
                if payload is None:
                    continue
                writer.add_message(ids[topic], stamp, payload, stamp, sequence=step)
                counts[topic] = counts.get(topic, 0) + 1
    finally:
        writer.finish()
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "messages": sum(counts.values()),
        "message_counts": counts,
        "encoding": encoding,
    }


def fixture_mcap_json(root: Path) -> dict[str, Any]:
    """Nominal bag, JSON channels: the shape the existing fixtures already cover."""
    return write_mcap(
        root / "mcap_json" / "bag.mcap",
        channels={
            "/joint_states": (50.0, "sensor_msgs/msg/JointState"),
            "/gripper/command": (50.0, "std_msgs/msg/Float64"),
        },
        messages_for=lambda step, topic: (
            json.dumps({"data": round(0.5 + 0.5 * math.sin(0.05 * step / 50.0), 6)}).encode()
            if topic == "/gripper/command"
            else _json_payload(step)
        ),
    )


def fixture_mcap_gripper_topic(root: Path) -> dict[str, Any]:
    """A bimanual robot whose busiest multi-dim topic is named `.../gripper/...`."""
    return write_mcap(
        root / "mcap_gripper_topic" / "bag.mcap",
        channels={"/left_gripper/joint_states": (50.0, "sensor_msgs/msg/JointState")},
        messages_for=lambda step, _topic: _json_payload(step),
    )


def fixture_mcap_cdr(root: Path) -> dict[str, Any]:
    """`ros2 bag convert` output: CDR-encoded payloads the engine refuses to decode."""
    return write_mcap(
        root / "mcap_cdr" / "bag.mcap",
        encoding="cdr",
        channels={"/joint_states": (50.0, "sensor_msgs/msg/JointState")},
        messages_for=lambda step, _topic: _cdr_joint_state(step),
    )


def fixture_mcap_protobuf(root: Path) -> dict[str, Any]:
    """Foxglove's other encoding."""
    return write_mcap(
        root / "mcap_protobuf" / "bag.mcap",
        encoding="protobuf",
        channels={"/tf": (10.0, "tf2_msgs/msg/TFMessage")},
        messages_for=lambda step, _topic: _protobuf_transform(step),
    )


def fixture_mcap_flat(root: Path) -> dict[str, Any]:
    """A real bag whose payload is a flat dict, not a nested JointState."""
    return write_mcap(
        root / "mcap_flat" / "bag.mcap",
        channels={"/odom": (50.0, "nav_msgs/msg/Odometry")},
        messages_for=lambda step, _topic: json.dumps(
            {
                "x": round(math.sin(step / 8.0), 6),
                "y": round(math.cos(step / 8.0), 6),
                "z": round(step / 200.0, 6),
            },
            separators=(",", ":"),
        ).encode("utf-8"),
    )


def fixture_mcap_ragged(root: Path) -> dict[str, Any]:
    """The joint vector changes width at t=10s: a mis-publisher, seen live."""
    return write_mcap(
        root / "mcap_ragged" / "bag.mcap",
        channels={"/joint_states": (50.0, "sensor_msgs/msg/JointState")},
        messages_for=lambda step, _topic: _json_payload(step, joints=6 if step < 500 else 4),
    )


def fixture_mcap_nonfinite(root: Path) -> dict[str, Any]:
    """NaN and Infinity in the payload - legal JSON, poison for a mean."""

    def payload(step: int, _topic: str) -> bytes:
        body = json.loads(_json_payload(step))
        if step == 250:
            body["position"][0] = float("nan")
        if step == 750:
            body["position"][1] = float("inf")
        return json.dumps(body, separators=(",", ":")).encode("utf-8")

    return write_mcap(
        root / "mcap_nonfinite" / "bag.mcap",
        channels={"/joint_states": (50.0, "sensor_msgs/msg/JointState")},
        messages_for=payload,
    )


def fixture_mcap_empty(root: Path) -> dict[str, Any]:
    """A valid file the recorder produced before it ever captured anything."""
    return write_mcap(
        root / "mcap_empty" / "bag.mcap",
        seconds=0.001,
        channels={"/joint_states": (50.0, "sensor_msgs/msg/JointState")},
        messages_for=lambda _step, _topic: None,
    )


def fixture_truncated_mcap(root: Path) -> dict[str, Any]:
    """A recorder killed mid-write: the last chunk is half a record."""
    source = root / "mcap_truncated" / "bag.mcap"
    write_mcap(
        source,
        channels={"/joint_states": (50.0, "sensor_msgs/msg/JointState")},
        messages_for=lambda step, _topic: _json_payload(step),
    )
    raw = source.read_bytes()
    source.write_bytes(raw[: int(len(raw) * 0.6)])
    return {"path": str(source), "bytes": source.stat().st_size}


def _cdr_joint_state(step: int) -> bytes:
    """A CDR-encoded `sensor_msgs/msg/JointState`: the bytes `ros2 bag` really writes."""
    out = bytearray(b"\x00\x01\x00\x00")
    names = [name.encode("utf-8") + b"\x00" for name in JOINTS]
    out += struct.pack("<I", len(names))
    for name in names:
        out += struct.pack("<I", len(name)) + name
    out += b"\x00" * ((4 - len(out) % 4) % 4)
    t = step / 50.0
    for index in range(len(JOINTS)):
        out += struct.pack("<d", math.sin(0.7 * t + 0.4 * index))
    for index in range(len(JOINTS)):
        out += struct.pack("<d", 0.7 * math.cos(0.7 * t + 0.4 * index))
    return bytes(out)


def _protobuf_transform(step: int) -> bytes:
    """A protobuf TFMessage: varint length-delimited child frames."""
    out = bytearray()
    t = step / 10.0
    for index in range(2):
        child = f"link_{index}".encode()
        out += b"\x0a" + _varint(len(child)) + child
        x = math.sin(t + index)
        out += b"\x09" + struct.pack("<d", x)
    return bytes(out)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def fixture_ros2_sqlite_bag(root: Path) -> dict[str, Any]:
    """The *default* `ros2 bag record` output: a sqlite3 file plus `metadata.yaml`."""
    import sqlite3

    directory = root / "ros2_sqlite_bag"
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / "robot_teleop.db3"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE topics (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                serialization_format TEXT NOT NULL,
                offered_qos_profiles TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                topic_id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                data BLOB NOT NULL
            );
            CREATE INDEX timestamp_index ON messages (timestamp);
            """
        )
        topics = [
            (1, "/joint_states", "sensor_msgs/msg/JointState", "cdr"),
            (2, "/gripper/command", "std_msgs/msg/Float64", "cdr"),
        ]
        connection.executemany(
            "INSERT INTO topics VALUES (?,?,?,?,?)",
            [(i, name, type_, ser, "[]") for i, name, type_, ser in topics],
        )
        start = 1_700_000_000 * NANOSECONDS_PER_SECOND
        rows = []
        for step in range(20 * 50):
            stamp = start + int((step / 50.0) * NANOSECONDS_PER_SECOND)
            rows.append((1, stamp, _cdr_joint_state(step)))
            if step % 5 == 0:
                rows.append((2, stamp, struct.pack("<I", 4) + struct.pack("<d", 0.5)))
        connection.executemany(
            "INSERT INTO messages (topic_id,timestamp,data) VALUES (?,?,?)", rows
        )
        connection.commit()
    finally:
        connection.close()
    joint_count = 20 * 50
    gripper_count = 20 * 10
    metadata = (
        "rosbag2_bagfile_information:\n"
        "  version: 5\n"
        "  storage_identifier: sqlite3\n"
        "  duration:\n    nanoseconds: 20000000000\n"
        f"  message_count: {len(rows)}\n"
        "  topics_with_message_count:\n"
        "    - topic_metadata:\n        name: /joint_states\n"
        "        type: sensor_msgs/msg/JointState\n"
        f"      message_count: {joint_count}\n"
        "    - topic_metadata:\n        name: /gripper/command\n"
        "        type: std_msgs/msg/Float64\n"
        f"      message_count: {gripper_count}\n"
        "  compression_format: ''\n"
        "  compression_mode: ''\n"
        "  relative_file_paths:\n    - robot_teleop.db3\n"
    )
    (directory / "metadata.yaml").write_text(metadata, encoding="utf-8")
    return {"path": str(directory), "bytes": db_path.stat().st_size, "messages": len(rows)}


def fixture_umi_hdf5(root: Path) -> dict[str, Any]:
    """UMI / handheld-gripper layout: `data/*.hdf5` with observations + action."""
    import h5py

    directory = root / "umi_hdf5"
    (directory / "data").mkdir(parents=True, exist_ok=True)
    demo_path = directory / "data" / "demo_0000.hdf5"
    frames = 400
    t = np.arange(frames, dtype=np.float64) / 20.0
    rng = np.arange(6, dtype=np.float64)
    qpos = np.stack([np.sin(0.7 * t + 0.4 * i) for i in rng], axis=1)
    qvel = np.stack([0.7 * np.cos(0.7 * t + 0.4 * i) for i in rng], axis=1)
    with h5py.File(demo_path, "w") as handle:
        obs = handle.create_group("observations")
        images = obs.create_group("images")
        for camera in ("cam", "left", "right"):
            images.create_dataset(camera, data=np.zeros((frames, 8, 8, 3), dtype=np.uint8))
        obs.create_dataset("qpos", data=qpos)
        obs.create_dataset("qvel", data=qvel)
        obs.create_dataset("gripper_state", data=np.stack([qpos[:, -1] * 0.1], axis=1))
        obs.create_dataset(
            "eef_pos",
            data=np.stack([t * 0.01, np.cos(t) * 0.02, np.sin(t) * 0.02], axis=1),
        )
        handle.create_dataset("action", data=qpos[:, :5])
        info = handle.create_group("info")
        info.attrs["start_frame"] = 0
        info.attrs["end_frame"] = frames - 1
        info.attrs["total_num_frames"] = frames
        info.attrs["fps"] = 20
    (directory / "task_description.json").write_text(
        json.dumps(
            {
                "task_description": "pour the beans into the bowl",
                "dataset": "umi_cup pour",
            }
        ),
        encoding="utf-8",
    )
    (directory / "episode_lengths.json").write_text(json.dumps([frames]), encoding="utf-8")
    return {"path": str(directory), "bytes": demo_path.stat().st_size, "frames": frames}


def fixture_rlds_tfds(root: Path) -> dict[str, Any]:
    """Open X-Embodiment's on-disk contract: RLDS/TFDS with `dataset_info.json`."""
    directory = root / "rlds_tfds" / "bridge_v2" / "1.0.0"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dataset_info.json").write_text(
        json.dumps(
            {
                "name": "bridge_orig",
                "version": "1.0.0",
                "features": {
                    "steps": {
                        "observation": {
                            "image": {"dtype": "uint8", "shape": [None, 256, 256, 3]},
                            "state": {"dtype": "float32", "shape": [7]},
                        },
                        "action": {"dtype": "float32", "shape": [7]},
                        "is_first": {"dtype": "bool", "shape": []},
                        "is_last": {"dtype": "bool", "shape": []},
                        "is_terminal": {"dtype": "bool", "shape": []},
                    }
                },
                "splits": {"train": "1.0.0"},
                "downloadSize": 1234,
                "datasetSize": 5678,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (directory / "features.json").write_text(
        json.dumps(
            {
                "featuresDict": {
                    "steps": {
                        "observation": {
                            "image": {
                                "dtype": "uint8",
                                "shape": [256, 256, 3],
                                "tensorShape": [None],
                            },
                            "state": {"dtype": "float32", "shape": [7], "tensorShape": [7]},
                        },
                        "action": {"dtype": "float32", "shape": [7], "tensorShape": [7]},
                    },
                    "episode_metadata": {
                        "episode_id": {"dtype": "int64"},
                        "episode_index": {"dtype": "int64"},
                    },
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    def record(payload: bytes) -> bytes:
        masked = _masked_crc32c(payload)
        return struct.pack("<Q", len(payload)) + masked + payload + masked

    def feature(key: str, value: bytes) -> bytes:
        return b"".join(
            [
                _varint(10),
                _varint(len(key)),
                key.encode("utf-8"),
                _varint(12),
                _varint(len(value)),
                value,
            ]
        )

    episode_id = b"episode_000000"
    state = struct.pack("<7f", *np.linspace(0.0, 1.0, 7, dtype=np.float32).tolist())
    payload = b"".join(
        [
            b"\x0a",
            _varint(len(feature("episode_id", episode_id))),
            feature("episode_id", episode_id),
        ]
    ) + feature("state", state)
    shards = directory / "bridge_orig-train.tfrecord-00000-of-00001"
    shards.write_bytes(record(payload) * 25)
    return {"path": str(directory), "bytes": shards.stat().st_size}


_CRC32C_POLY = 0x82F63B78
_CRC32C_TABLE: dict[int, int] = {}


def _crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        if not _CRC32C_TABLE:
            for index in range(256):
                value = index
                for _ in range(8):
                    value = (value >> 1) ^ (_CRC32C_POLY if value & 1 else 0)
                _CRC32C_TABLE[index] = value
        crc = (crc >> 8) ^ _CRC32C_TABLE[(crc ^ byte) & 0xFF]
    return crc ^ 0xFFFFFFFF


def _masked_crc32c(data: bytes) -> bytes:
    crc = _crc32c(data)
    rotated = ((crc >> 15) | (crc << 17)) + 0xA282EAD8
    return struct.pack("<I", rotated & 0xFFFFFFFF)


def fixture_zarr_droid(root: Path) -> dict[str, Any]:
    """A DROID-style zarr store: v2 layout, so plain JSON + raw chunks on disk."""
    directory = root / "zarr_droid"
    for group, shape, chunks in (
        ("joint_action", (400, 7), (100, 7)),
        ("joint_states/position", (400, 7), (100, 7)),
        ("observation/camera0", (400, 1, 1, 3), (100, 1, 1, 3)),
    ):
        part = directory / group
        part.mkdir(parents=True, exist_ok=True)
        (part / ".zarray").write_text(
            json.dumps(
                {
                    "chunks": list(chunks),
                    "compressor": None,
                    "dtype": "<f4" if "camera" not in group else "|u1",
                    "fill_value": 0,
                    "filters": None,
                    "order": "C",
                    "shape": list(shape),
                    "zarr_format": 2,
                }
            ),
            encoding="utf-8",
        )
        (part / ".zattrs").write_text(json.dumps({"_ARRAY_DIMENSIONS": ["t"]}), encoding="utf-8")
        rows = math.prod(chunks)
        dtype = np.float32 if "camera" not in group else np.uint8
        (part / "0.0").write_bytes(np.arange(rows, dtype=dtype).tobytes())
    (directory / "meta").mkdir(exist_ok=True)
    (directory / "meta" / "episode_lengths.json").write_text(json.dumps([400]), encoding="utf-8")
    total = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
    return {"path": str(directory), "bytes": total}


def fixture_webdataset_tar(root: Path) -> dict[str, Any]:
    """Tars of per-step files - what a dozen imitation-learning repos ship."""
    directory = root / "webdataset"
    directory.mkdir(parents=True, exist_ok=True)
    steps = 100
    for shard in range(2):
        path = directory / f"episode_000000-{shard:06d}.tar"
        with tarfile.open(path, "w") as archive:
            for step in range(shard * steps, (shard + 1) * steps):
                prefix = f"{step:08d}"
                for name, array in (
                    ("observation/state", np.linspace(0, 1, 7, dtype=np.float32)),
                    ("action", np.linspace(0, 1, 7, dtype=np.float32)),
                ):
                    buffer = io.BytesIO()
                    np.save(buffer, array, allow_pickle=False)
                    payload = buffer.getvalue()
                    info = tarfile.TarInfo(f"{prefix}.{name}.npy")
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
    return {"path": str(directory), "bytes": sum(f.stat().st_size for f in directory.glob("*.tar"))}


def fixture_npz_flat(root: Path) -> dict[str, Any]:
    """One `.npz` of arrays: the oldest robot dataset layout, still everywhere."""
    path = root / "npz_flat" / "episode_0.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(400, dtype=np.float64) / 20.0
    np.savez(
        path,
        qpos=np.stack([np.sin(0.7 * t + 0.4 * i) for i in range(6)], axis=1),
        qvel=np.stack([0.7 * np.cos(0.7 * t + 0.4 * i) for i in range(6)], axis=1),
        action=np.stack([np.sin(0.7 * t + 0.4 * i) for i in range(5)], axis=1),
        task=np.array("wipe the table"),
    )
    return {"path": str(path), "bytes": path.stat().st_size}


def fixture_lerobot_v4(root: Path) -> dict[str, Any]:
    """A `meta/info.json` claiming a codebase_version this reader does not know."""
    directory = root / "lerobot_v4"
    (directory / "meta").mkdir(parents=True, exist_ok=True)
    (directory / "meta" / "info.json").write_text(
        json.dumps(
            {
                "codebase_version": "v4.0",
                "robot_type": "so101_follower",
                "total_episodes": 1,
                "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            }
        ),
        encoding="utf-8",
    )
    return {"path": str(directory)}


def fixture_lerobot_no_meta(root: Path) -> dict[str, Any]:
    """Half an unzip: `data/` present, `meta/info.json` missing."""
    directory = root / "lerobot_no_meta"
    (directory / "data" / "chunk-000").mkdir(parents=True, exist_ok=True)
    table = pa.table({"frame_index": np.arange(5), "observation.state": [np.zeros(6)] * 5})
    pq.write_table(table, directory / "data" / "chunk-000" / "episode_000000.parquet")
    return {"path": str(directory)}


def fixture_video_only(root: Path) -> dict[str, Any]:
    """A camera dataset: mp4 shards and a sidecar, no parquet at all."""
    directory = root / "video_only"
    videos = directory / "videos" / "observation.images.front"
    videos.mkdir(parents=True, exist_ok=True)
    for index in range(2):
        header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
        (videos / f"episode_{index:06d}.mp4").write_bytes(header + b"\x00" * 4096)
    (directory / "info.json").write_text(
        json.dumps({"video": True, "fps": 30, "frames": 600}), encoding="utf-8"
    )
    return {"path": str(directory)}


def fixture_not_a_dataset(root: Path) -> dict[str, Any]:
    """The mistaken submission: a source tree, not recordings."""
    directory = root / "not_a_dataset"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "README.txt").write_text("robot teleop analysis\n" * 60_000, encoding="utf-8")
    (directory / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
    return {"path": str(directory), "bytes": (directory / "README.txt").stat().st_size}


def fixture_zip_containing_mcap(root: Path) -> dict[str, Any]:
    """A `.zip` of a bag: how recordings arrive when someone sends you a drive."""
    inner = root / "_zip_inner" / "bag.mcap"
    write_mcap(
        inner,
        channels={"/joint_states": (50.0, "sensor_msgs/msg/JointState")},
        messages_for=lambda step, _topic: _json_payload(step),
    )
    path = root / "zip_containing_mcap" / "teleop.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(inner, arcname="bag.mcap")
    return {"path": str(path), "bytes": path.stat().st_size}


FIXTURES = (
    ("mcap_json", fixture_mcap_json),
    ("mcap_gripper_topic", fixture_mcap_gripper_topic),
    ("mcap_cdr", fixture_mcap_cdr),
    ("mcap_protobuf", fixture_mcap_protobuf),
    ("mcap_flat", fixture_mcap_flat),
    ("mcap_ragged", fixture_mcap_ragged),
    ("mcap_nonfinite", fixture_mcap_nonfinite),
    ("mcap_empty", fixture_mcap_empty),
    ("truncated_mcap", fixture_truncated_mcap),
    ("ros2_sqlite_bag", fixture_ros2_sqlite_bag),
    ("umi_hdf5", fixture_umi_hdf5),
    ("rlds_tfds", fixture_rlds_tfds),
    ("zarr_droid", fixture_zarr_droid),
    ("webdataset_tar", fixture_webdataset_tar),
    ("npz_flat", fixture_npz_flat),
    ("lerobot_v4", fixture_lerobot_v4),
    ("lerobot_no_meta", fixture_lerobot_no_meta),
    ("video_only", fixture_video_only),
    ("zip_containing_mcap", fixture_zip_containing_mcap),
    ("not_a_dataset", fixture_not_a_dataset),
)

MANIFEST_NAME = "corpus.json"


def build(root: Path) -> dict[str, Any]:
    """Write every fixture under `root` and return the manifest."""
    root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for name, builder in FIXTURES:
        try:
            summary = builder(root)
            entries.append({"name": name, "status": "built", **summary})
        except Exception as exc:
            entries.append(
                {"name": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
            )
    manifest = {"root": str(root), "fixtures": entries}
    (root / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def load(root: Path) -> list[dict[str, Any]]:
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    return list(manifest["fixtures"])


def main() -> None:
    parser = argparse.ArgumentParser(prog="foreign_data_corpus")
    parser.add_argument("root", type=Path, nargs="?", default=Path("var/foreign-corpus"))
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="print the existing manifest instead of rebuilding it",
    )
    args = parser.parse_args()
    if args.manifest_only:
        print(json.dumps(load(args.root), indent=2, sort_keys=True))
        return
    manifest = build(args.root)
    for entry in manifest["fixtures"]:
        mark = "ok " if entry["status"] == "built" else "ERR"
        print(f"{mark} {entry['name']:<22} {entry.get('bytes', 0):>10} B  {entry.get('path', '')}")
    failed = [e for e in manifest["fixtures"] if e["status"] != "built"]
    if failed:
        print(f"\n{len(failed)} fixture(s) failed to build:", file=sys.stderr)
        for entry in failed:
            print(f"  {entry['name']}: {entry.get('error')}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
