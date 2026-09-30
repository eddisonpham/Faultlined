"""MCAP sensor-log reader: one file is one episode.

MCAP is the container Foxglove standardised on and the one `ros2 bag convert` writes,
so it is the natural input for the raw end of the pipeline (ADR 0006). It is also the
one raw format with no frame structure at all - a bag is a set of *topics* with
independent rates, and nothing in the file says "this is an episode". Two decisions
follow, and both are visible in the output rather than hidden in a default:

- **A file is an episode.** MCAP has no episode index to slice the way a LeRobot v3
  dataset has, so `episode_key` must either match the file's own key or be absent; a
  request for `episode_index=3` against a bag is an error naming what the file
  actually holds, not a silent read of the wrong span (`lerobot.py` takes the same
  position on an ambiguous key).
- **Frame rate is the busiest topic's rate.** A bag publishes joint states at 50 Hz and
  images at 10 Hz; summing them would produce a number nothing downstream can use. The
  busiest topic is the closest thing MCAP has to a frame rate, and it is reported as
  such. The rate is measured as *(messages - 1) / span* - intervals between
  consecutive messages, which is what a 50 Hz stream actually is; `messages / span`
  reports 50.05 Hz for the same stream because it counts a period that never elapsed.
- **Motion quality is scored on the busiest topic that has more than one dimension.**
  A scalar topic (a gripper setpoint, a single setpoint) is excluded from the verdict by
  `analyze` anyway - discrete and gripper dimensions never judge motion - so preferring
  it would report `unknown` for logs that do contain the signal. Among candidates the
  message count wins, then the dimension count, then the topic name, so the choice is
  reproducible.

Only channels whose `message_encoding` is `json` are decoded into numbers. CDR
(`ros2msg`) and protobuf payloads are counted and reported but not interpreted: a
guessed struct layout would put wrong numbers in the catalog, which is worse than an
honest gap. Their count, rate, and duration are still exact, so channel-presence and
frame-count rules work on a ROS 2 bag today.

The pass is streaming. Per-channel statistics are running accumulators and the quality
sample is a decimated window (`_Window`), so peak memory is a function of the number of
topics, not of the length of the log (`architecture/storage.md` §1).
"""

from __future__ import annotations

import json
import math
import struct
import zlib
from pathlib import Path
from typing import Any

from mcap.exceptions import McapError
from mcap.reader import make_reader

from data_engine.analysis.quality import analyze
from data_engine.ingest.readers.base import ChannelStats, EpisodeExtraction, ReaderError

MAGIC = b"\x89MCAP0\r\n"
NANOSECONDS_PER_SECOND = 1_000_000_000

#: Payloads this reader will interpret. Everything else is counted, not decoded.
DECODABLE_ENCODINGS = frozenset({"json"})

#: Values kept per dimension for motion analysis before the window starts decimating.
QUALITY_WINDOW = 512

#: Metadata keys the engine looks for, in order of preference.
_ROBOT_KEYS = ("robot_type", "robot")
_TASK_KEYS = ("task", "task_name")


class _Window:
    """Bounded sample of one dimension, decimated in place as it fills.

    Motion character survives decimation; unbounded memory does not. Halving the
    values and doubling the stride every time the window overflows bounds peak memory
    at `2 * QUALITY_WINDOW` floats per dimension while keeping the sample evenly
    spread over the whole log, which a first-N window would not.
    """

    __slots__ = ("stride", "values")

    def __init__(self) -> None:
        self.values: list[float] = []
        self.stride = 1

    def add(self, value: float) -> None:
        self.values.append(value)
        if len(self.values) > 2 * QUALITY_WINDOW:
            self.values = self.values[::2]
            self.stride *= 2


class _Topic:
    """Everything one MCAP channel contributes, accumulated in a single pass."""

    __slots__ = (
        "encoding",
        "first",
        "highest",
        "last",
        "lowest",
        "messages",
        "numbers",
        "reference",
        "schema_id",
        "schema_name",
        "squares",
        "topic",
        "total",
        "windows",
    )

    def __init__(self, topic: str, encoding: str, schema_name: str, schema_id: int) -> None:
        self.topic = topic
        self.encoding = encoding
        self.schema_name = schema_name
        self.schema_id = schema_id
        self.messages = 0
        self.numbers = 0
        self.total = 0.0
        self.squares = 0.0
        self.lowest = math.inf
        self.highest = -math.inf
        self.first = 0
        self.last = 0
        self.reference: tuple[str, ...] = ()
        self.windows: dict[str, _Window] = {}

    def stamp(self, log_time: int) -> None:
        if self.messages == 0:
            self.first = self.last = log_time
        else:
            self.first = min(self.first, log_time)
            self.last = max(self.last, log_time)
        self.messages += 1

    def span_seconds(self) -> float:
        return max((self.last - self.first) / NANOSECONDS_PER_SECOND, 0.0)

    def observe(self, payload: Any) -> None:
        dims = _dimensions(payload)
        if not dims:
            return
        for value in dims.values():
            self.numbers += 1
            self.total += value
            self.squares += value * value
            self.lowest = min(self.lowest, value)
            self.highest = max(self.highest, value)
        self._sample(dims)

    def _sample(self, dims: dict[str, float]) -> None:
        """Feed the quality window, keeping every dimension the same length.

        The first decodable message fixes the dimension set. Later messages are used
        only if they carry all of it, because `analyze` rejects ragged series and a
        half-filled window would misreport a gap as a jitter spike.
        """
        if not self.reference:
            self.reference = tuple(sorted(dims))
            self.windows = {name: _Window() for name in self.reference}
        elif any(name not in dims for name in self.reference):
            return
        for name in self.reference:
            self.windows[name].add(dims[name])

    def rate(self) -> float | None:
        span = self.span_seconds()
        if span <= 0 or self.messages < 2:
            return None
        return round((self.messages - 1) / span, 3)

    def stats(self) -> ChannelStats:
        count = self.numbers
        mean = self.total / count if count else None
        # Population sigma, matching the LeRobot reader's computed statistics.
        spread = math.sqrt(max(0.0, self.squares / count - mean**2)) if mean is not None else None
        return ChannelStats(
            name=self.topic,
            dtype=self.schema_name or self.encoding,
            count=self.messages,
            min=None if count == 0 else self.lowest,
            max=None if count == 0 else self.highest,
            mean=mean,
            std=spread,
        )

    def quality_series(self) -> dict[str, list[float]]:
        return {f"{self.topic}.{name}": window.values for name, window in self.windows.items()}


class McapReader:
    """Reads one recording out of an MCAP file."""

    format = "mcap"

    def sniff(self, path: Path) -> bool:
        """True for a file whose first bytes are the MCAP magic. Never raises."""
        try:
            with path.open("rb") as stream:
                return stream.read(len(MAGIC)) == MAGIC
        except OSError:
            return False

    def read(self, path: Path, *, episode_key: str | None = None) -> EpisodeExtraction:
        key = episode_key_for(path)
        if episode_key is not None and episode_key != key:
            raise ReaderError(
                f"{path} holds exactly one episode, {key}; it cannot be read as {episode_key!r}"
            )
        try:
            with path.open("rb") as stream:
                return self._describe(path, key, make_reader(stream))
        except ReaderError:
            raise
        except (McapError, OSError, ValueError, struct.error, zlib.error) as exc:
            raise ReaderError(f"cannot read MCAP {path}: {exc}") from exc

    def _describe(self, path: Path, key: str, reader: Any) -> EpisodeExtraction:
        header = reader.get_header()
        topics: dict[int, _Topic] = {}
        schemas: dict[int, str] = {}
        messages = 0
        earliest = latest = 0
        seen_any = False

        for schema, channel, message in reader.iter_messages(log_time_order=False):
            topic = topics.get(channel.id)
            if topic is None:
                topic = _Topic(
                    channel.topic,
                    channel.message_encoding,
                    schema.name if schema is not None else "",
                    channel.schema_id,
                )
                topics[channel.id] = topic
            if schema is not None:
                schemas[schema.id] = schema.name
            topic.stamp(message.log_time)
            messages += 1
            if not seen_any or message.log_time < earliest:
                earliest = message.log_time
            if not seen_any or message.log_time > latest:
                latest = message.log_time
            seen_any = True
            if topic.encoding.lower() in DECODABLE_ENCODINGS:
                topic.observe(_loads(message.data))

        span = max((latest - earliest) / NANOSECONDS_PER_SECOND, 0.0) if seen_any else 0.0
        busiest = _busiest(topics)
        judged = _scorable(topics)
        facts, records = _metadata_facts(reader)
        task = _first(facts, _TASK_KEYS)
        return EpisodeExtraction(
            format=self.format,
            format_version=header.profile or "unprofiled",
            episode_key=key,
            source_path=path,
            robot_type=_first(facts, _ROBOT_KEYS) or "unknown",
            task=task,
            tasks=(task,) if task else (),
            frame_count=messages,
            duration_seconds=span,
            fps=busiest.rate() if busiest is not None else None,
            channels=tuple(
                topic.stats() for topic in sorted(topics.values(), key=lambda t: t.topic)
            ),
            quality=analyze(judged.quality_series()) if judged is not None else None,
            dataset={
                "root": str(path),
                "profile": header.profile,
                "library": header.library,
                "message_count": messages,
                "channel_count": len(topics),
                "schema_count": len(schemas),
                "decoded_channels": sorted(
                    topic.topic
                    for topic in topics.values()
                    if topic.encoding.lower() in DECODABLE_ENCODINGS
                ),
                "undecodable_channels": sorted(
                    topic.topic
                    for topic in topics.values()
                    if topic.encoding.lower() not in DECODABLE_ENCODINGS
                ),
                "attachments": [
                    {"name": item.name, "media_type": item.media_type, "bytes": len(item.data)}
                    for item in reader.iter_attachments()
                ],
                "metadata_records": records,
                "fps_source_topic": busiest.topic if busiest is not None else None,
                "quality_source_topic": judged.topic if judged is not None else None,
            },
        )


def episode_key_for(path: Path) -> str:
    """The one key a single-recording file can answer to."""
    return f"file={path.name}"


def _busiest(topics: dict[int, _Topic]) -> _Topic | None:
    """Highest-rate topic, ties broken by name so the choice is reproducible."""
    if not topics:
        return None
    return min(topics.values(), key=lambda topic: (-topic.messages, topic.topic))


def _scorable(topics: dict[int, _Topic]) -> _Topic | None:
    """The topic motion quality is read from: busiest, then widest, then first by name."""
    candidates = [topic for topic in topics.values() if len(topic.reference) > 1]
    if not candidates:
        return None
    return min(candidates, key=lambda t: (-t.messages, -len(t.reference), t.topic))


def _dimensions(value: Any, prefix: str = "") -> dict[str, float] | None:
    """Flatten a decoded JSON message into `path -> number`.

    Non-numeric leaves are dropped rather than failing the message: a joint-state
    message carries a `name` list of strings next to its positions, and refusing to
    score it because of that would throw away the signal. Returns None only when
    there is no number at all.
    """
    if value is None or isinstance(value, bool):
        return {}
    if isinstance(value, int | float):
        return {prefix or "value": float(value)}
    if isinstance(value, str):
        return {}
    if isinstance(value, dict):
        out: dict[str, float] = {}
        for key, item in value.items():
            out.update(_dimensions(item, f"{prefix}.{key}" if prefix else str(key)) or {})
        return out
    if isinstance(value, list | tuple):
        out = {}
        for index, item in enumerate(value):
            out.update(_dimensions(item, f"{prefix}[{index}]") or {})
        return out
    return {}


def _loads(data: bytes) -> Any:
    """Decode a message body, or None when it is not the JSON this reader expects."""
    try:
        return json.loads(data)
    except UnicodeDecodeError, json.JSONDecodeError:
        return None


def _metadata_facts(reader: Any) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Flat `key -> value` facts, and the records they came from.

    MCAP carries no field for "which robot" or "what task"; it has `Metadata` records
    for exactly this. First value wins in file order, so a file with two records
    declaring the same key is read the same way every time.
    """
    facts: dict[str, str] = {}
    records: list[dict[str, str]] = []
    for record in reader.iter_metadata():
        entry = {str(key): str(value) for key, value in (record.metadata or {}).items()}
        records.append({"name": record.name, **entry})
        for key, value in entry.items():
            facts.setdefault(key, value)
    return facts, records


def _first(facts: dict[str, str], keys: tuple[str, ...]) -> str:
    return next((facts[key] for key in keys if facts.get(key)), "")


__all__ = ["MAGIC", "QUALITY_WINDOW", "McapReader", "episode_key_for"]
