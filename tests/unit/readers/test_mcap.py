"""MCAP reader tests.

Offline only. Every fixture is written by `scripts/make_mcap_log.py` or by a
hand-built bag in this module, so the structural rules are asserted against exact
values with no network: which topic becomes the frame rate, which one motion quality
is read from, what a non-JSON channel contributes, and what a corrupt file does.

The generator is imported rather than shelled out to because the reader's correctness
and the benchmark's input are the same artefact; a test that regenerated the log
differently from the benchmark would be testing a different format than it claims.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

import pytest
from mcap.writer import CompressionType, Writer

from data_engine.ingest.readers.base import ChannelStats, ReaderError, finite_or_none
from data_engine.ingest.readers.mcap_reader import (
    MAGIC,
    QUALITY_WINDOW,
    McapReader,
    _dimensions,
    _to_float,
    _Topic,
)
from data_engine.ingest.readers.registry import read_episode, reader_for

pytestmark = [pytest.mark.unit]

_SECONDS_PER_SECOND = 1_000_000_000
_START_NANOS = 1_700_000_000 * _SECONDS_PER_SECOND


# --------------------------------------------------------------------- dispatch


def test_sniff_claims_only_magic_bytes(tmp_path: Path) -> None:
    bag = tmp_path / "log.mcap"
    bag.write_bytes(MAGIC + b"rest")
    other = tmp_path / "notes.mcap"
    other.write_bytes(b"not a bag")
    assert McapReader().sniff(bag) is True
    assert McapReader().sniff(other) is False


def test_sniff_is_false_for_a_directory_and_a_missing_file(tmp_path: Path) -> None:
    assert McapReader().sniff(tmp_path) is False
    assert McapReader().sniff(tmp_path / "absent.mcap") is False


def test_registry_dispatches_a_bag_to_the_mcap_reader(mcap_log: Any, tmp_path: Path) -> None:
    bag = tmp_path / "log.mcap"
    mcap_log.write_log(bag, seconds=0.2)
    assert isinstance(reader_for(bag), McapReader)
    assert read_episode(bag).format == "mcap"


def test_registry_error_names_the_formats_it_tried(tmp_path: Path) -> None:
    unknown = tmp_path / "mystery.bin"
    unknown.write_bytes(b"\x00\x01\x02")
    with pytest.raises(ReaderError, match="mcap, lerobot"):
        read_episode(unknown)


# ------------------------------------------------------------------ description


def test_describes_a_generated_log_exactly(mcap_log: Any, tmp_path: Path) -> None:
    bag = tmp_path / "so101.mcap"
    summary = mcap_log.write_log(bag, seconds=2.0)
    episode = read_episode(bag)

    assert episode.format == "mcap"
    assert episode.format_version == "unprofiled"
    assert episode.episode_key == "file=so101.mcap"
    assert episode.robot_type == "so101_follower"
    assert episode.task == "pick_place"
    assert episode.frame_count == sum(summary["message_counts"].values())
    assert episode.duration_seconds == pytest.approx(1.98)
    assert {channel.name for channel in episode.channels} == set(summary["message_counts"])
    assert episode.dataset["message_count"] == episode.frame_count
    assert episode.dataset["channel_count"] == 5
    assert episode.dataset["metadata_records"] == [
        {"name": "session", "robot_type": "so101_follower", "task": "pick_place"}
    ]


def test_frame_rate_is_the_busiest_topics_interval_rate(mcap_log: Any, tmp_path: Path) -> None:
    """`(messages - 1) / span` counts periods, so a 50 Hz stream reads 50.0, not 50.05."""
    episode = read_episode(_generated(mcap_log, tmp_path, seconds=4.0))
    assert episode.fps == pytest.approx(50.0)
    assert episode.dataset["fps_source_topic"] in {"/joint_states", "/gripper/command"}


def test_quality_reads_the_widest_busiest_topic(mcap_log: Any, tmp_path: Path) -> None:
    """`/gripper/command` ties on message count but carries one excluded dimension."""
    episode = read_episode(_generated(mcap_log, tmp_path, seconds=4.0))
    assert episode.dataset["quality_source_topic"] == "/joint_states"
    assert episode.quality is not None
    assert episode.quality.verdict != "unknown"
    assert len(episode.quality.dims) == 18


def test_channel_statistics_are_exact(mcap_log: Any, tmp_path: Path) -> None:
    """Recomputed from the generator's own formulas, so the reader is not trusted twice."""
    episode = read_episode(_generated(mcap_log, tmp_path, seconds=2.0))
    joints = next(channel for channel in episode.channels if channel.name == "/joint_states")
    numbers: list[float] = []
    for step in range(100):
        message = mcap_log.joint_state(step / 50.0)
        numbers.extend(message["position"] + message["velocity"] + message["effort"])

    assert joints.count == 100
    assert joints.min == pytest.approx(min(numbers))
    assert joints.max == pytest.approx(max(numbers))
    assert joints.mean == pytest.approx(sum(numbers) / len(numbers))
    assert joints.std == pytest.approx(
        (sum(value**2 for value in numbers) / len(numbers) - (sum(numbers) / len(numbers)) ** 2)
        ** 0.5
    )
    assert joints.dtype == "sensor_msgs/msg/JointState"


def test_string_leaves_do_not_stop_a_message_being_scored(tmp_path: Path) -> None:
    """A joint-state message carries names; refusing it would throw the signal away."""
    bag = _bag(
        tmp_path / "named.mcap",
        {"/joints": (20.0, [{"name": ["a", "b"], "position": [1.0, -1.0]}] * 20)},
    )
    episode = read_episode(bag)
    joints = next(channel for channel in episode.channels if channel.name == "/joints")
    assert joints.count == 20
    assert quality_dims(episode) == ["/joints.position[0]", "/joints.position[1]"]


# ------------------------------------------------------------------- selection


def test_a_bag_rejects_an_episode_key_it_cannot_satisfy(tmp_path: Path) -> None:
    bag = _bag(tmp_path / "one.mcap", {"/t": (10.0, [{"v": 1.0}])})
    with pytest.raises(ReaderError, match="holds exactly one episode"):
        read_episode(bag, episode_key="episode_index=3")
    assert read_episode(bag, episode_key="file=one.mcap").episode_key == "file=one.mcap"


def test_quality_falls_back_when_no_topic_has_more_than_one_dimension(tmp_path: Path) -> None:
    bag = _bag(tmp_path / "scalar.mcap", {"/gripper": (10.0, [{"data": 0.5}] * 10)})
    episode = read_episode(bag)
    assert episode.dataset["quality_source_topic"] is None
    assert episode.quality is None
    assert episode.fps == pytest.approx(10.0)


def test_an_empty_log_is_described_not_rejected(tmp_path: Path) -> None:
    """Validation owns the EMPTY_EPISODE verdict; a reader that raised would hide it."""
    bag = tmp_path / "empty.mcap"
    writer = Writer(str(bag), compression=CompressionType.NONE)
    writer.start(profile="", library="test")
    writer.finish()
    episode = read_episode(bag)
    assert episode.frame_count == 0
    assert episode.duration_seconds == 0.0
    assert episode.fps is None
    assert episode.channels == ()


# ------------------------------------------------------------- undecodable data


def test_a_non_json_channel_is_counted_but_not_interpreted(tmp_path: Path) -> None:
    """A guessed CDR layout would put wrong numbers in the catalog."""
    bag = _bag(
        tmp_path / "cdr.mcap",
        {"/scan": (10.0, [b""] * 10, "ros2msg")},
    )
    episode = read_episode(bag)
    scan = next(channel for channel in episode.channels if channel.name == "/scan")
    assert scan.count == 10
    assert (scan.min, scan.max, scan.mean, scan.std) == (None, None, None, None)
    assert episode.dataset["decoded_channels"] == []
    assert episode.dataset["undecodable_channels"] == ["/scan"]
    assert episode.fps == pytest.approx(10.0)


def test_a_corrupt_json_payload_does_not_fail_the_ingest(tmp_path: Path) -> None:
    bag = _bag(tmp_path / "ragged.mcap", {"/j": (10.0, [b"{not json", b'{"a":1}'])})
    episode = read_episode(bag)
    joints = next(channel for channel in episode.channels if channel.name == "/j")
    assert joints.count == 2
    assert episode.quality is None  # one dimension only, so nothing to judge


def test_messages_with_different_shapes_do_not_produce_ragged_series(tmp_path: Path) -> None:
    bag = _bag(
        tmp_path / "shapes.mcap",
        {"/j": (10.0, [{"a": 1.0, "b": 2.0}, {"a": 3.0}, {"a": 5.0, "b": 6.0}])},
    )
    episode = read_episode(bag)
    assert episode.quality is not None
    # The two-dimension message set the reference; the one-field message is dropped.
    assert episode.quality.frame_count == 2


# ------------------------------------------------------------------- streaming


def test_the_quality_window_is_bounded_however_long_the_log(mcap_log: Any, tmp_path: Path) -> None:
    """Ingest must not grow with episode length, so the window decimates in place."""
    short = read_episode(_generated(mcap_log, tmp_path, seconds=2.0))
    long = read_episode(_generated(mcap_log, tmp_path, seconds=40.0))
    assert short.quality is not None and long.quality is not None
    assert short.quality.frame_count == 100
    assert QUALITY_WINDOW < long.quality.frame_count <= 2 * QUALITY_WINDOW
    assert long.frame_count == 4_840  # 2000 + 2000 joints/gripper, 400 + 400 tf/camera, 40 diag
    assert long.quality.verdict in {"smooth", "moderate", "jerky"}


# --------------------------------------------------------------------- failures


def test_a_truncated_bag_is_a_reader_error(tmp_path: Path) -> None:
    bag = _bag(tmp_path / "whole.mcap", {"/t": (10.0, [{"v": float(i)} for i in range(50)])})
    whole = bag.read_bytes()
    bag.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(ReaderError, match="cannot read MCAP"):
        read_episode(bag)


def test_a_file_without_the_magic_is_a_reader_error(tmp_path: Path) -> None:
    """Sniff keeps the registry away from it; a direct call still has to fail cleanly."""
    bag = tmp_path / "fake.mcap"
    bag.write_bytes(struct.pack("<Q", 0) * 4)
    with pytest.raises(ReaderError, match="cannot read MCAP"):
        McapReader().read(bag)


def test_a_directory_is_a_reader_error(tmp_path: Path) -> None:
    with pytest.raises(ReaderError, match="cannot read MCAP"):
        McapReader().read(tmp_path)


# ------------------------------------------------------------------- fixtures


def _generated(generator: Any, tmp_path: Path, *, seconds: float) -> Path:
    bag = tmp_path / f"log-{seconds}.mcap"
    generator.write_log(bag, seconds=seconds)
    return bag


def quality_dims(episode: Any) -> list[str]:
    return [dim.name for dim in episode.quality.dims] if episode.quality else []


def _bag(
    path: Path,
    topics: dict[str, tuple[Any, ...]],
    default_encoding: str = "json",
) -> Path:
    """One topic per entry; the list is the message bodies, already encoded or not.

    Bodies are spaced at the requested rate, so `len(bodies)` messages span
    `(len(bodies) - 1) / rate` seconds and the reader's interval-based rate reads back
    as `rate` exactly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = Writer(str(path), compression=CompressionType.NONE)
    writer.start(profile="", library="test")
    try:
        for topic, (rate, bodies, *rest) in topics.items():
            encoding = rest[0] if rest else default_encoding
            schema_id = writer.register_schema(topic, encoding, b"{}")
            channel_id = writer.register_channel(topic, encoding, schema_id)
            period = round(_SECONDS_PER_SECOND / rate)
            for index, body in enumerate(bodies):
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                stamp = _START_NANOS + index * period
                writer.add_message(channel_id, stamp, payload, stamp)
    finally:
        writer.finish()
    return path


@pytest.mark.unit
def test_a_log_much_longer_than_the_quality_window_is_not_reported_as_gapped(
    tmp_path: Path,
) -> None:
    """The regression that made this signal untrustworthy the first time.

    The clock buffer used to be halved with `[::2]`, in lockstep with the value
    windows. That is correct for values - their statistics ignore order and
    spacing - and wrong for a clock: sample 0 survives every halving, so the
    buffer ended up as one ancient timestamp followed by a dense block of recent
    ones, and the gap detector read the distance between them as a dropped
    recording. A clean 50 Hz log was reported as having a 501-second hole.

    Four times the window, so the buffer is trimmed at least three times and the
    residue has somewhere to go.
    """
    rate = 50.0
    count = 8 * QUALITY_WINDOW
    bodies = [{"position": [j, j, j]} for j in range(count)]
    path = _bag(tmp_path / "long.mcap", {"/joint_states": (rate, bodies)})

    quality = read_episode(path).quality

    assert quality is not None
    assert quality.integrity == "ok", quality.max_gap_seconds
    # The whole-log maximum is measured on the raw stream, so it is the true
    # inter-message interval and not a distance between decimated survivors.
    assert quality.max_gap_seconds == pytest.approx(1 / rate, rel=1e-3)
    assert quality.gap_ratio == 0.0


@pytest.mark.unit
def test_a_real_drop_before_the_retained_window_is_still_reported(tmp_path: Path) -> None:
    """The bounded window cannot see this; the exact whole-log maximum can.

    A freeze in the first tenth of a long log is discarded by the time the buffer
    fills. Reporting `ok` there would be the same lie in the opposite direction,
    so the reader keeps the largest interval it ever saw in constant memory and
    hands it to the analysis.
    """
    rate = 50.0
    count = 8 * QUALITY_WINDOW
    period = round(1e9 / rate)
    freeze_after = count // 10
    path = tmp_path / "dropped.mcap"
    writer = Writer(str(path), compression=CompressionType.NONE)
    writer.start(profile="", library="test")
    try:
        topic = "/joint_states"
        schema_id = writer.register_schema(topic, "json", b"{}")
        channel_id = writer.register_channel(topic, "json", schema_id)
        for index in range(count):
            body = json.dumps({"position": [index, index, index]}).encode()
            offset = period * index + (30 * 1e9 if index >= freeze_after else 0)
            writer.add_message(channel_id, int(offset), body, int(offset))
    finally:
        writer.finish()

    quality = read_episode(path).quality

    assert quality is not None
    assert quality.integrity == "gapped"
    assert quality.max_gap_seconds == pytest.approx(30.0, rel=1e-3)


# ---------------------------------------------------- compiled flatten plan
#
# The plan is a speed trick, and speed tricks are where behaviour quietly
# changes. The oracle is the dict path the plan replaced: `_observe_dims` is
# still shipped code (the fallback), so the fast path is held to bit-identical
# output against the very implementation it stands in for.


def _state(topic: Any) -> tuple[Any, ...]:
    stats = topic.stats()
    series = {name: tuple(values) for name, values in topic.quality_series().items()}
    return (
        stats.count,
        stats.min,
        stats.max,
        stats.mean,
        stats.std,
        topic.reference,
        series,
        tuple(topic.quality_clock() or ()),
        topic.widest_interval(),
    )


_EQUIVALENCE_PAYLOADS = [
    {"position": [1.0, 2.0], "velocity": [0.1, 0.2], "name": ["a", "b"]},
    {"position": [1.5, 2.5], "velocity": [0.3, 0.4], "name": ["a", "b"]},
    # Same keys, different order: traversal order changed, so the plan must
    # rebuild (floats do not add associatively and the dict path follows order).
    {"name": ["a", "b"], "position": [1.5, 2.5], "velocity": [0.3, 0.4]},
    # Ragged: missing a reference name.
    {"position": [9.0]},
    # Extra numeric name beyond the reference set.
    {"position": [2.0, 3.0], "velocity": [0.5, 0.6], "name": ["a", "b"], "effort": [7.0]},
    # Leaf kind flip: velocity is numeric no more -> ragged again.
    {"position": [2.0, 3.0], "velocity": ["x", "y"], "name": ["a", "b"]},
    # List length change is a shape change.
    {
        "position": [3.0, 4.0],
        "velocity": [0.7, 0.8],
        "name": ["a", "b"],
        "effort": [8.0, 9.0, 10.0],
    },
    # Deep nesting, bool/str/None leaves, and a number float cannot hold.
    {"nested": {"a": {"b": [1, 2]}, "c": True}, "s": "str", "n": None, "big": 10**400},
    [1, 2, [3, 4]],
    {"mixed": [{"x": 1}, {"x": 2}]},
    42,
    {"a": (1, 2)},
    {"position": [1.0, 2.0], "velocity": [0.1, 0.2], "name": ["a", "b"]},
]


def test_the_plan_path_and_the_dict_path_agree_on_every_message() -> None:
    fast = _Topic("/j", "json", "state", 1)
    slow = _Topic("/j", "json", "state", 1)
    for index, payload in enumerate(_EQUIVALENCE_PAYLOADS):
        stamp = _START_NANOS + index * 20_000_000
        fast.observe(payload, stamp)
        slow._observe_dims(_dimensions(payload), stamp)
        assert _state(fast) == _state(slow), f"diverged at message {index}: {payload!r}"


def test_the_plan_is_compiled_once_per_shape() -> None:
    """The win is the plan being reused; this pins that it actually is."""
    topic = _Topic("/j", "json", "state", 1)
    topic.observe({"a": [1.0, 2.0]}, _START_NANOS)
    plan = topic.plan
    assert plan is not None
    for index in range(1, 5):
        topic.observe({"a": [1.0 + index, 2.0]}, _START_NANOS + index * 20_000_000)
    assert topic.plan is plan


def test_a_changed_shape_replans_and_stays_consistent() -> None:
    fast = _Topic("/j", "json", "state", 1)
    slow = _Topic("/j", "json", "state", 1)
    messages = [{"a": [1.0]}, {"a": [2.0], "b": 3.0}, {"a": [4.0], "b": 5.0}]
    for index, payload in enumerate(messages):
        stamp = _START_NANOS + index * 20_000_000
        fast.observe(payload, stamp)
        slow._observe_dims(_dimensions(payload), stamp)
    assert set(fast.names) == {"a[0]", "b"}
    assert _state(fast) == _state(slow)


def test_a_number_float_cannot_hold_is_dropped_not_fatal() -> None:
    """JSON parses a 400-digit integer; one leaf must not fail the episode."""
    assert _to_float(10**400) is None
    assert _dimensions({"x": 10**400, "y": 2.0}) == {"y": 2.0}
    topic = _Topic("/j", "json", "state", 1)
    topic.observe({"x": 10**400, "y": 2.0}, _START_NANOS)
    assert topic.numbers == 1
    assert topic.stats().max == 2.0


def test_an_unrepresentable_number_survives_the_whole_read(tmp_path: Path) -> None:
    bag = _bag(
        tmp_path / "huge.mcap",
        {"/j": (10.0, [{"a": 10**400, "b": 2.0}, {"a": 10**400, "b": 3.0}])},
    )
    episode = read_episode(bag)
    joints = next(channel for channel in episode.channels if channel.name == "/j")
    assert joints.count == 2
    assert joints.max == 3.0


@pytest.mark.unit
def test_a_non_finite_sample_is_counted_not_accumulated(tmp_path: Path) -> None:
    """One `Infinity` must not fail the read, and must not vanish (EXP-0014 D2).

    Before this, the infinite sample made every channel statistic infinite and
    `register_episode`'s `jsonb` write rejected the whole episode, which the
    worker classified retryable and retried three times over the same bytes.
    """
    bag = _bag(
        tmp_path / "inf.mcap",
        {"/j": (10.0, [{"a": float("inf"), "b": 2.0}, {"a": 4.0, "b": 3.0}])},
    )
    episode = read_episode(bag)
    joints = next(channel for channel in episode.channels if channel.name == "/j")
    assert joints.count == 2
    assert joints.nonfinite == 1
    assert joints.max == 4.0
    # The event reaches the quality signal rather than being silently dropped:
    # a non-finite value is an absence of a measurement (ADR 0023).
    assert episode.quality is not None
    assert episode.quality.nonfinite == 1
    assert episode.quality.verdict == "unknown"
    # And nothing about to be written to `jsonb` is non-finite.
    payload = joints.to_dict()
    assert all(
        payload[field] is None or math.isfinite(payload[field])
        for field in ("min", "max", "mean", "std")
    )


@pytest.mark.unit
def test_finite_or_none_replaces_unrepresentable_statistics() -> None:
    stats = ChannelStats(
        name="/j", dtype="json", count=2, min=0.0, max=float("inf"), mean=float("nan"), std=1.0
    )
    payload = stats.to_dict()
    assert payload["max"] is None
    assert payload["mean"] is None
    assert payload["min"] == 0.0
    assert payload["std"] == 1.0
    assert finite_or_none(None) is None
    assert finite_or_none(float("inf")) is None
    assert finite_or_none(2.5) == 2.5
    # A LeRobot published vector is sanitized element-wise, not passed whole.
    assert finite_or_none([0.5, float("inf"), 2.0]) == [0.5, None, 2.0]
