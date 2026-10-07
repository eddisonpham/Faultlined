"""Generate a deterministic MCAP sensor log - the project's real-data fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from mcap.writer import CompressionType, Writer

TOPICS: dict[str, tuple[float, str, str]] = {
    "/joint_states": (50.0, "sensor_msgs/msg/JointState", "json"),
    "/gripper/command": (50.0, "std_msgs/msg/Float64", "json"),
    "/tf": (10.0, "tf2_msgs/msg/TFMessage", "json"),
    "/camera/color/image_meta": (10.0, "sensor_msgs/msg/Image", "json"),
    "/diagnostics": (1.0, "diagnostic_msgs/msg/DiagnosticArray", "json"),
}

JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")

NANOSECONDS_PER_SECOND = 1_000_000_000


def joint_state(t: float) -> dict[str, object]:
    """Six joints driven by two sine mixtures, so the motion has real character."""
    return {
        "name": list(JOINTS),
        "position": [round(math.sin(0.7 * t + 0.4 * i), 6) for i in range(6)],
        "velocity": [round(0.7 * math.cos(0.7 * t + 0.4 * i), 6) for i in range(6)],
        "effort": [round(1.5 * math.sin(0.15 * t + i), 6) for i in range(6)],
    }


def gripper_command(t: float) -> dict[str, float]:
    return {"data": round(0.5 + 0.5 * math.sin(0.05 * t), 6)}


def tf_message(t: float) -> dict[str, object]:
    return {
        "transforms": [
            {
                "header": {"stamp": t, "frame_id": "base_link"},
                "child_frame_id": "tool",
                "translation": {
                    "x": round(0.3 * math.sin(0.4 * t), 6),
                    "y": round(0.3 * math.cos(0.4 * t), 6),
                    "z": round(0.1 * t, 6),
                },
                "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            }
        ]
    }


def image_meta(t: float) -> dict[str, object]:
    return {
        "header": {"stamp": t, "frame_id": "camera"},
        "height": 480,
        "width": 640,
        "encoding": "rgb8",
        "step": 1920,
    }


def diagnostics(t: float) -> dict[str, object]:
    return {
        "status": [
            {"name": "joints", "level": 0, "message": f"ok at {t:.3f}s", "hardware_id": "so101"}
        ]
    }


MESSAGES: dict[str, object] = {
    "/joint_states": joint_state,
    "/gripper/command": gripper_command,
    "/tf": tf_message,
    "/camera/color/image_meta": image_meta,
    "/diagnostics": diagnostics,
}


def write_log(
    path: Path,
    *,
    seconds: float = 20.0,
    start_nanos: int = 1_700_000_000 * NANOSECONDS_PER_SECOND,
    robot_type: str = "so101_follower",
    task: str = "pick_place",
    compression: CompressionType = CompressionType.ZSTD,
    attachment_mib: float = 0.0,
) -> dict[str, object]:
    """Write one bag and return a description of what is in it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ids: dict[str, int] = {}
    counts: dict[str, int] = {}
    writer = Writer(str(path), compression=compression)
    try:
        writer.start(profile="", library="faultlined-make-mcap-log")
        writer.add_metadata("session", {"robot_type": robot_type, "task": task})
        if attachment_mib > 0:
            size = int(attachment_mib * 1024 * 1024)
            writer.add_attachment(
                create_time=start_nanos,
                log_time=start_nanos,
                name="camera/color/compressed",
                media_type="application/octet-stream",
                data=bytes(size),
            )
        for topic, (_, schema_name, encoding) in TOPICS.items():
            schema_id = writer.register_schema(
                schema_name, encoding, json.dumps({"topic": topic}).encode("utf-8")
            )
            ids[topic] = writer.register_channel(topic, encoding, schema_id)

        samples = max(1, int(seconds * 50))
        for step in range(samples):
            t = step / 50.0
            stamp = start_nanos + int(t * NANOSECONDS_PER_SECOND)
            for topic, (rate, _schema_name, _encoding) in TOPICS.items():
                if step % max(1, round(50.0 / rate)):
                    continue
                payload = json.dumps(MESSAGES[topic](t), separators=(",", ":")).encode("utf-8")
                writer.add_message(ids[topic], stamp, payload, stamp, sequence=step)
                counts[topic] = counts.get(topic, 0) + 1
    finally:
        writer.finish()

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": digest,
        "seconds": seconds,
        "compression": compression.name,
        "robot_type": robot_type,
        "task": task,
        "message_counts": counts,
        "attachment_mib": attachment_mib,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="make_mcap_log")
    parser.add_argument("path", type=Path)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--compression", choices=("zstd", "lz4", "none"), default="zstd")
    parser.add_argument(
        "--attachment-mib",
        type=float,
        default=0.0,
        help="add one binary attachment of this size, as a camera log would carry",
    )
    args = parser.parse_args()
    requested = {
        "zstd": CompressionType.ZSTD,
        "lz4": CompressionType.LZ4,
        "none": CompressionType.NONE,
    }[args.compression]
    try:
        summary = write_log(
            args.path,
            seconds=args.seconds,
            compression=requested,
            attachment_mib=args.attachment_mib,
        )
    except Exception as exc:
        raise SystemExit(
            f"could not write {args.path} with {args.compression} compression: {exc}\n"
            "zstd and lz4 are optional extras of the mcap distribution; "
            "re-run with --compression none if neither is installed."
        ) from exc
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
