"""MCAP sensor-log reader: one file is one episode."""

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

DECODABLE_ENCODINGS = frozenset({"json"})

QUALITY_WINDOW = 512

MIN_QUALITY_FRAMES = 2

_ROBOT_KEYS = ("robot_type", "robot")
_TASK_KEYS = ("task", "task_name")


class _Window:
    """Bounded sample of one dimension, decimated in place as it fills."""

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
        "buffer",
        "clock",
        "encoding",
        "first",
        "highest",
        "last",
        "lowest",
        "messages",
        "names",
        "nonfinite",
        "numbers",
        "plan",
        "ref",
        "reference",
        "schema_id",
        "schema_name",
        "squares",
        "topic",
        "total",
        "widest",
        "windows",
        "wins",
    )

    def __init__(self, topic: str, encoding: str, schema_name: str, schema_id: int) -> None:
        self.topic = topic
        self.encoding = encoding
        self.schema_name = schema_name
        self.schema_id = schema_id
        self.messages = 0
        self.numbers = 0
        self.nonfinite = 0
        """Samples that were NaN or infinite: counted, never accumulated."""
        self.total = 0.0
        self.squares = 0.0
        self.lowest = math.inf
        self.highest = -math.inf
        self.first = 0
        self.last = 0
        self.reference: tuple[str, ...] = ()
        self.windows: dict[str, _Window] = {}
        self.plan: tuple[Any, ...] | None = None
        """Compiled flatten plan for the current message shape, or None."""
        self.names: tuple[str, ...] = ()
        """Dimension path per plan slot, in traversal order."""
        self.buffer: list[float | None] = []
        """One slot per leaf: the value when numeric this message, else None."""
        self.ref: tuple[int, ...] = ()
        """Slot holding each reference name's value, in reference order."""
        self.wins: list[_Window] = []
        """The `windows` values in reference order, without per-message lookups."""
        self.widest = 0.0
        """Exact largest interval between consecutive accepted samples, whole log."""
        self.clock: list[float] = []
        """Log times of the messages that fed the quality window, decimated with it."""

    def stamp(self, log_time: int) -> None:
        if self.messages == 0:
            self.first = self.last = log_time
        else:
            self.first = min(self.first, log_time)
            self.last = max(self.last, log_time)
        self.messages += 1

    def span_seconds(self) -> float:
        return max((self.last - self.first) / NANOSECONDS_PER_SECOND, 0.0)

    def observe(self, payload: Any, log_time: int = 0) -> None:
        """Accumulate one decoded message."""
        plan = self.plan
        if plan is None or not _run(plan, payload, self.buffer):
            self._replan(payload)
            plan = self.plan
            if plan is None or not _run(plan, payload, self.buffer):
                self._observe_dims(_dimensions(payload), log_time)
                return
        seen = False
        for value in self.buffer:
            if value is None:
                continue
            seen = True
            if not math.isfinite(value):
                self.nonfinite += 1
                continue
            self.numbers += 1
            self.total += value
            self.squares += value * value
            if value < self.lowest:
                self.lowest = value
            if value > self.highest:
                self.highest = value
        if not seen:
            return
        if not self.reference:
            names = sorted(
                name
                for name, value in zip(self.names, self.buffer, strict=True)
                if value is not None
            )
            self.reference = tuple(names)
            self.windows = {name: _Window() for name in names}
            self.wins = [self.windows[name] for name in names]
            self._rebind()
        self._sample_slots(log_time)

    def _replan(self, payload: Any) -> None:
        """Compile the flatten plan for this message's shape and rebind names."""
        names: list[str] = []
        self.plan = _compile(payload, "", names)
        self.names = tuple(names)
        self.buffer = [None] * len(names)
        self._rebind()

    def _rebind(self) -> None:
        """Point the reference names at their slots in the current plan."""
        index = {name: i for i, name in enumerate(self.names)}
        self.ref = tuple(index.get(name, -1) for name in self.reference)

    def _sample_slots(self, log_time: int) -> None:
        """Feed the quality window from the slot buffer."""
        buffer = self.buffer
        for slot in self.ref:
            if slot < 0 or buffer[slot] is None:
                return
        for window, slot in zip(self.wins, self.ref, strict=True):
            value = buffer[slot]
            if value is not None:
                window.add(value)
        self._decimate_clock(log_time)

    def _observe_dims(self, dims: dict[str, float], log_time: int = 0) -> None:
        """The dict-shaped path: identical semantics to the plan, no plan needed."""
        if not dims:
            return
        for value in dims.values():
            if not math.isfinite(value):
                self.nonfinite += 1
                continue
            self.numbers += 1
            self.total += value
            self.squares += value * value
            if value < self.lowest:
                self.lowest = value
            if value > self.highest:
                self.highest = value
        if not self.reference:
            self.reference = tuple(sorted(dims))
            self.windows = {name: _Window() for name in self.reference}
            self.wins = [self.windows[name] for name in self.reference]
            self._rebind()
        if any(name not in dims for name in self.reference):
            return
        for name in self.reference:
            self.windows[name].add(dims[name])
        self._decimate_clock(log_time)

    def _decimate_clock(self, log_time: int) -> None:
        stamp = log_time / 1e9
        if self.clock:
            self.widest = max(self.widest, stamp - self.clock[-1])
        self.clock.append(stamp)
        if len(self.clock) > 2 * QUALITY_WINDOW:
            keep = (len(self.clock) + 1) // 2
            self.clock = self.clock[len(self.clock) - keep :]

    def rate(self) -> float | None:
        span = self.span_seconds()
        if span <= 0 or self.messages < 2:
            return None
        return round((self.messages - 1) / span, 3)

    def stats(self) -> ChannelStats:
        count = self.numbers
        mean = self.total / count if count else None
        spread = math.sqrt(max(0.0, self.squares / count - mean**2)) if mean is not None else None
        return ChannelStats(
            name=self.topic,
            dtype=self.schema_name or self.encoding,
            count=self.messages,
            min=None if count == 0 else self.lowest,
            max=None if count == 0 else self.highest,
            mean=mean,
            std=spread,
            nonfinite=self.nonfinite,
        )

    def quality_series(self) -> dict[str, list[float]]:
        return {f"{self.topic}.{name}": window.values for name, window in self.windows.items()}

    def widest_interval(self) -> float:
        """Exact largest gap between consecutive accepted samples, over the whole log."""
        return self.widest

    def quality_clock(self) -> list[float] | None:
        """Timestamps aligned with `quality_series`, or None when nothing was sampled."""
        if not self.windows or not self.clock:
            return None
        length = min(len(self.clock), min(len(w.values) for w in self.windows.values()))
        if length < MIN_QUALITY_FRAMES:
            return None
        return self.clock[:length]


class McapReader:
    """Reads one recording out of an MCAP file."""

    format = "mcap"

    def sniff(self, path: Path) -> bool:
        """True for a file whose first bytes are the MCAP magic."""
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
                topic.observe(_loads(message.data), message.log_time)

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
            quality=(
                analyze(
                    judged.quality_series(),
                    timestamps=judged.quality_clock(),
                    max_interval_seconds=judged.widest_interval(),
                )
                if judged is not None
                else None
            ),
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


def _to_float(value: int | float) -> float | None:
    """`float()`, but an unrepresentable number is unmeasurable, not fatal."""
    try:
        return float(value)
    except OverflowError:
        return None


def _compile(value: Any, prefix: str, names: list[str]) -> tuple[Any, ...]:
    """Compile a flatten plan for one message shape; names are built once, ever."""
    if isinstance(value, dict):
        keys = tuple(value)
        return (
            "d",
            keys,
            tuple(
                _compile(item, f"{prefix}.{key}" if prefix else str(key), names)
                for key, item in value.items()
            ),
        )
    if isinstance(value, list | tuple):
        return (
            "l",
            len(value),
            tuple(_compile(item, f"{prefix}[{index}]", names) for index, item in enumerate(value)),
        )
    names.append(prefix or "value")
    return ("v", len(names) - 1)


def _run(node: tuple[Any, ...], value: Any, slots: list[float | None]) -> bool:
    """Fill `slots` per the plan."""
    kind = node[0]
    if kind == "d":
        if not isinstance(value, dict) or len(value) != len(node[2]):
            return False
        for (key, item), expected, child in zip(value.items(), node[1], node[2], strict=True):
            if key != expected:
                return False
            if not _run(child, item, slots):
                return False
        return True
    if kind == "l":
        if not isinstance(value, list | tuple) or len(value) != node[1]:
            return False
        ok = True
        for item, child in zip(value, node[2], strict=True):
            if not _run(child, item, slots):
                ok = False
                break
        return ok
    slot = node[1]
    exact = type(value)
    if exact is float:
        slots[slot] = value
    elif exact is int:
        slots[slot] = _to_float(value)
    elif value is None or exact is str or exact is bool:
        slots[slot] = None
    elif isinstance(value, dict | list | tuple):
        return False
    elif isinstance(value, int | float):
        slots[slot] = _to_float(value)
    else:
        slots[slot] = None
    return True


def _dimensions(value: Any, prefix: str = "") -> dict[str, float]:
    """Flatten a decoded JSON message into `path -> number`."""
    if value is None or isinstance(value, bool):
        return {}
    if isinstance(value, int | float):
        converted = _to_float(value)
        return {} if converted is None else {prefix or "value": converted}
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
    """Flat `key -> value` facts, and the records they came from."""
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
