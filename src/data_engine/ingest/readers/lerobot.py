"""LeRobot dataset reader, covering both the v2.x and v3.0 on-disk layouts.

Why one reader and not two: `codebase_version` in `meta/info.json` is the *only*
discriminator the format itself defines, and the two layouts are structurally
different, not cosmetically different.

    v2.1   data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet
           episodes indexed in meta/episodes.jsonl, stats in meta/episodes_stats.jsonl

    v3.0   data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet
           episodes indexed in meta/episodes/chunk-*/file-*.parquet, sliced by the
           dataset_from_index / dataset_to_index row range, with per-episode stats as
           stats/<feature>/{min,max,mean,std,count} columns

A v3 episode is a *row range*, not a file. Those indices are global across the whole
dataset, so slicing one file needs the file's starting row subtracted - see
`_file_offset`. Getting that wrong returns well-formed frames from the wrong episode,
which is the worst failure mode available here, so an inconsistent index is a hard
error rather than a best guess.

Both layouts are implemented against real Hub artifacts; the fixtures are named in
`agents/research/technology-matrix.md` (re-verification pass 2026-09-29).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from data_engine.analysis.quality import analyze
from data_engine.ingest.readers.base import ChannelStats, EpisodeExtraction, ReaderError

INFO_RELATIVE_PATH = Path("meta") / "info.json"
SUPPORTED_VERSIONS = ("v2.0", "v2.1", "v3.0")
V2_CHUNKS_SIZE = 1000

# Columns the engine interprets directly; anything else is a feature column.
_STRUCTURAL_COLUMNS = frozenset(
    {"timestamp", "frame_index", "episode_index", "index", "task_index", "next.done"}
)

# (chunk_index, file_index) -> first global row of that data file, for v3 datasets.
_FileRef = tuple[int, int]


class LeRobotReader:
    """Reads one episode out of a LeRobot dataset directory."""

    format = "lerobot"

    def sniff(self, path: Path) -> bool:
        try:
            return (path / INFO_RELATIVE_PATH).is_file()
        except OSError:
            return False

    def read(self, path: Path, *, episode_key: str | None = None) -> EpisodeExtraction:
        info = self._read_info(path)
        version = str(info.get("codebase_version") or "")
        if version not in SUPPORTED_VERSIONS:
            raise ReaderError(
                f"unsupported LeRobot codebase_version {version or '(missing)'!r}; "
                f"expected one of {', '.join(SUPPORTED_VERSIONS)}"
            )
        index = self._episode_index(path, version)
        if not index:
            raise ReaderError(f"{path} declares no episodes")

        key, record = self._select(index, episode_key)
        source = self._locate(path, version, record)
        table = self._read_rows(source, version, record, index)
        return self._to_extraction(
            path=path,
            info=info,
            version=version,
            record=record,
            key=key,
            source=source,
            table=table,
        )

    # ------------------------------------------------------------------ metadata

    def _read_info(self, root: Path) -> dict[str, Any]:
        target = root / INFO_RELATIVE_PATH
        if not target.is_file():
            raise ReaderError(f"not a LeRobot dataset: {target} is missing")
        try:
            info = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReaderError(f"unreadable {target}: {exc}") from exc
        if not isinstance(info, dict):
            raise ReaderError(f"{target} must contain a JSON object")
        return info

    def _episode_index(self, root: Path, version: str) -> dict[int, dict[str, Any]]:
        """Map episode_index -> record, from wherever this version keeps them."""
        if version.startswith("v2"):
            return self._index_v2(root)
        return self._index_v3(root)

    def _index_v2(self, root: Path) -> dict[int, dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {}
        for line in self._jsonl(root / "meta" / "episodes.jsonl"):
            key = line.get("episode_index")
            if key is not None:
                rows[int(key)] = line
        return rows

    def _index_v3(self, root: Path) -> dict[int, dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {}
        directory = root / "meta" / "episodes"
        for episode_file in sorted(directory.rglob("*.parquet")) if directory.is_dir() else []:
            for record in pq.read_table(episode_file).to_pylist():
                key = record.get("episode_index")
                if key is not None:
                    rows[int(key)] = record
        return rows

    @staticmethod
    def _jsonl(target: Path) -> list[dict[str, Any]]:
        if not target.is_file():
            return []
        parsed: list[dict[str, Any]] = []
        for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReaderError(f"{target}:{number} is not valid JSON: {exc}") from exc
            if isinstance(value, dict):
                parsed.append(value)
        return parsed

    @staticmethod
    def _select(index: dict[int, dict[str, Any]], episode_key: str | None) -> tuple[int, Any]:
        """Pick the requested episode, or the first one. Never guesses silently."""
        if episode_key is None:
            first = min(index)
            return first, index[first]
        wanted = _parse_episode_key(key=episode_key)
        if wanted not in index:
            raise ReaderError(f"episode {wanted} is not in the dataset (have {sorted(index)})")
        return wanted, index[wanted]

    # -------------------------------------------------------------------- layout

    def _locate(self, root: Path, version: str, record: dict[str, Any]) -> Path:
        if version.startswith("v2"):
            episode_index = int(record["episode_index"])
            chunk = int(record.get("episode_chunk", episode_index // V2_CHUNKS_SIZE))
            candidate = (
                root / "data" / f"chunk-{chunk:03d}" / f"episode_{episode_index:06d}.parquet"
            )
        else:
            try:
                chunk = int(record["data/chunk_index"])
                file_index = int(record["data/file_index"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ReaderError(f"v3 episode record has no data file locator: {exc}") from exc
            candidate = root / "data" / f"chunk-{chunk:03d}" / f"file-{file_index:03d}.parquet"
        if not candidate.is_file():
            raise ReaderError(f"episode data file is missing: {candidate}")
        return candidate

    def _read_rows(
        self, source: Path, version: str, record: dict[str, Any], index: dict[int, Any]
    ) -> pa.Table:
        try:
            table = pq.read_table(source)
        except (OSError, ValueError) as exc:
            raise ReaderError(f"cannot read Parquet {source}: {exc}") from exc
        if version.startswith("v2"):
            return table
        return self._slice_v3(table, record, index, source)

    def _slice_v3(
        self, table: pa.Table, record: dict[str, Any], index: dict[int, Any], source: Path
    ) -> pa.Table:
        """Translate a v3 global row range into a local slice of one file."""
        try:
            start = int(record["dataset_from_index"])
            stop = int(record["dataset_to_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReaderError(f"v3 episode record is missing a dataset row range: {exc}") from exc
        if stop < start:
            raise ReaderError(f"v3 episode row range is inverted: [{start}, {stop})")

        offset = _file_offset(record, index)
        local_start = start - offset
        local_stop = local_start + (stop - start)
        if local_start < 0 or local_stop > table.num_rows:
            raise ReaderError(
                f"episode {record.get('episode_index')} maps to rows "
                f"[{local_start}, {local_stop}) of {source}, which holds {table.num_rows}"
            )
        return table.slice(local_start, local_stop - local_start)

    # ---------------------------------------------------------------- extraction

    def _to_extraction(
        self,
        *,
        path: Path,
        info: dict[str, Any],
        version: str,
        record: dict[str, Any],
        key: int,
        source: Path,
        table: pa.Table,
    ) -> EpisodeExtraction:
        tasks = _as_tasks(record.get("tasks"))
        timestamps = _column(table, "timestamp")
        t_start = timestamps[0] if timestamps else 0.0
        t_end = timestamps[-1] if timestamps else 0.0

        return EpisodeExtraction(
            format=f"lerobot-{_major(version)}",
            format_version=version,
            episode_key=f"episode_index={key}",
            source_path=source,
            robot_type=str(info.get("robot_type") or "unknown"),
            task=tasks[0] if tasks else "",
            tasks=tasks,
            frame_count=table.num_rows,
            duration_seconds=max(t_end - t_start, 0.0),
            fps=_as_float(info.get("fps")),
            channels=_channels(table, record),
            quality=analyze(_series(table), timestamps=_column(table, "timestamp")),
            dataset={
                "root": str(path),
                "codebase_version": version,
                "robot_type": str(info.get("robot_type") or "unknown"),
                "total_episodes": info.get("total_episodes"),
                "total_frames": info.get("total_frames"),
                "total_tasks": info.get("total_tasks"),
                "features": sorted(str(name) for name in (info.get("features") or {})),
                "declared_episode_length": record.get("length"),
                "has_video": _has_video(info),
                "dataset_from_index": record.get("dataset_from_index"),
                "dataset_to_index": record.get("dataset_to_index"),
            },
        )


# --------------------------------------------------------------------- helpers


def _file_offset(record: dict[str, Any], index: dict[int, Any]) -> int:
    """Global row index at which this episode's data file begins.

    v3 concatenates every data file into one logical table, so `dataset_from_index`
    is dataset-global. The start of a file is the sum of the lengths of every episode
    stored in an *earlier* file. Episodes are ordered by (chunk_index, file_index),
    which is the order the files are written in.
    """
    mine = _file_ref(record)
    per_file: dict[_FileRef, int] = {}
    for other in index.values():
        ref = _file_ref(other)
        per_file[ref] = per_file.get(ref, 0) + int(other.get("length") or 0)
    starts: dict[_FileRef, int] = {}
    running = 0
    for ref in sorted(per_file):
        starts[ref] = running
        running += per_file[ref]
    if mine not in starts:
        raise ReaderError(f"episode {record.get('episode_index')} references an unlisted data file")
    return starts[mine]


def _file_ref(record: dict[str, Any]) -> _FileRef:
    try:
        return (int(record["data/chunk_index"]), int(record["data/file_index"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ReaderError(f"v3 episode record has no data file locator: {exc}") from exc


def _major(version: str) -> str:
    return "v3" if version.startswith("v3") else "v2"


def _parse_episode_key(key: str) -> int:
    """Accept `3`, `episode_index=3`, or a full artifact id ending in `=3`."""
    tail = key.rsplit("=", 1)[-1]
    try:
        return int(tail)
    except ValueError as exc:
        raise ReaderError(f"cannot parse an episode index from {key!r}") from exc


def _as_tasks(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value if str(item))
    return (str(value),)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _series(table: pa.Table) -> dict[str, list[float]]:
    """Per-dimension frame series for quality analysis (ADR 0018).

    List-typed feature columns expand into `name[i]` dims. Structural columns,
    byte-string image columns, and columns whose rows disagree on width yield
    nothing — a quality signal must never fail an ingest.
    """
    out: dict[str, list[float]] = {}
    for name in table.schema.names:
        if name in _STRUCTURAL_COLUMNS:
            continue
        flat_rows = [_flatten(row) for row in table.column(name).to_pylist()]
        width = len(flat_rows[0]) if flat_rows else 0
        if width == 0 or any(len(row) != width for row in flat_rows):
            continue
        for i in range(width):
            key = name if width == 1 else f"{name}[{i}]"
            out[key] = [row[i] for row in flat_rows]
    return out


def _column(table: pa.Table, name: str) -> list[float]:
    if name not in table.schema.names:
        return []
    try:
        return [float(value) for value in table.column(name).to_pylist() if value is not None]
    except TypeError, ValueError:
        return []


def _channels(table: pa.Table, record: dict[str, Any]) -> tuple[ChannelStats, ...]:
    """Per-feature stats, preferring the format's own published values.

    v3 stores per-episode stats as `stats/<feature>/<agg>` columns; v2 keeps them in
    `meta/episodes_stats.jsonl`, which this reader does not yet join. When nothing is
    published the values are computed from the slice, so the contract also holds for
    hand-made datasets.
    """
    published = {
        name: values
        for name, values in _published_stats(record).items()
        if name not in _STRUCTURAL_COLUMNS
    }
    found = [
        _channel(table, name, published.pop(name, {}))
        for name in table.schema.names
        if name not in _STRUCTURAL_COLUMNS
    ]
    # Features the episode index describes but the data file does not carry (video
    # shards, mostly) are still reported: their absence is itself a fact.
    found.extend(
        ChannelStats(
            name=name,
            dtype="published",
            count=_count(values.get("count")),
            min=_first(values.get("min")),
            max=_first(values.get("max")),
            mean=_first(values.get("mean")),
            std=_first(values.get("std")),
        )
        for name, values in sorted(published.items())
    )
    return tuple(found)


def _published_stats(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for key, value in record.items():
        parts = key.split("/")
        if len(parts) == 3 and parts[0] == "stats":
            _, feature, aggregate = parts
            grouped.setdefault(feature, {})[aggregate] = value
    return grouped


def _channel(table: pa.Table, name: str, published: dict[str, Any]) -> ChannelStats:
    values = _flatten(table.column(name).to_pylist())
    return ChannelStats(
        name=name,
        dtype=str(table.schema.field(name).type),
        count=_count(published.get("count")) or len(values),
        min=_first(published.get("min")) if published else _extreme(values, min),
        max=_first(published.get("max")) if published else _extreme(values, max),
        mean=_first(published.get("mean")) if published else _extreme(values, _mean),
        std=_first(published.get("std")) if published else _std(values),
    )


def _count(value: Any) -> int:
    """`stats/<feature>/count` is a one-element list holding the count, not a sample."""
    try:
        return int(_first(value))
    except TypeError, ValueError:
        return 0


def _flatten(value: Any) -> list[float]:
    """Numbers only, recursively; byte-string image columns yield nothing."""
    if isinstance(value, list | tuple):
        out: list[float] = []
        for item in value:
            out.extend(_flatten(item))
        return out
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [float(value)]
    return []


def _first(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return value[0] if value else None
    return value


def _extreme(values: list[float], function: Any) -> float | None:
    return function(values) if values else None


def _mean(values: list[float]) -> float | None:
    """None for a column with no numbers in it, e.g. a list-of-strings feature."""
    return sum(values) / len(values) if values else None


def _std(values: list[float]) -> float | None:
    mean = _mean(values)
    if mean is None:
        return None
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return float(variance**0.5)


def _has_video(info: dict[str, Any]) -> bool:
    features = info.get("features") or {}
    return any(str(name).startswith(("observation.images", "image", "video")) for name in features)


__all__ = ["SUPPORTED_VERSIONS", "LeRobotReader"]
