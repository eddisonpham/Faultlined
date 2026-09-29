"""LeRobot reader tests.

Two layers, deliberately:

* **Offline** — hand-built directories that reproduce each on-disk layout, so the
  structural rules (version detection, v3 row-range slicing, stats preference) are
  tested with no network and with values small enough to assert exactly.
* **Real** — the actual Hub datasets named in `agents/research/technology-matrix.md`.
  These are the tests that would catch a reader that is confidently wrong about the
  format, which is the failure this module exists to prevent. Marked `network`; they
  skip cleanly when offline.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data_engine.ingest.readers.base import ReaderError
from data_engine.ingest.readers.lerobot import LeRobotReader
from data_engine.ingest.readers.registry import read_episode, reader_for

pytestmark = [pytest.mark.unit]


# --------------------------------------------------------------------- builders


def _info(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "codebase_version": "v3.0",
        "robot_type": "so100_follower",
        "fps": 30,
        "total_episodes": 2,
        "total_frames": 5,
        "total_tasks": 1,
        "features": {"action": {"dtype": "float32", "shape": [2]}},
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
    }
    base.update(overrides)
    return base


def _write_json(target: Path, value: Any) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value), encoding="utf-8")


def _write_parquet(target: Path, table: pa.Table) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, target)


def _frames(count: int, offset: int = 0) -> pa.Table:
    return pa.table(
        {
            "action": [[float(offset + i), float(offset + i) * 2] for i in range(count)],
            "timestamp": [float(offset + i) for i in range(count)],
            "frame_index": list(range(offset, offset + count)),
            "episode_index": [0] * count,
            "index": list(range(offset, offset + count)),
            "task_index": [0] * count,
        }
    )


def _v2_dataset(root: Path, *, version: str = "v2.1") -> Path:
    _write_json(
        root / "meta" / "info.json",
        _info(
            codebase_version=version,
            data_path="data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            total_episodes=2,
        ),
    )
    (root / "meta" / "episodes.jsonl").write_text(
        "\n".join(
            json.dumps({"episode_index": i, "length": 3, "tasks": [f"task {i}"]}) for i in range(2)
        ),
        encoding="utf-8",
    )
    for episode in range(2):
        _write_parquet(root / "data" / "chunk-000" / f"episode_{episode:06d}.parquet", _frames(3))
    return root


def _v3_dataset(root: Path, *, two_files: bool = False) -> Path:
    """A v3.0 dataset: episodes are row ranges, and they are packed into shared files.

    Mirrors the real layout — the published `lerobot/svla_so101_pickplace` puts all 50
    episodes in one `file-000.parquet` — so the second episode always starts mid-file.
    """
    _write_json(root / "meta" / "info.json", _info(total_episodes=2))
    if two_files:
        # Two data files, so the second episode's global row range has to be rebased
        # against a file that does not start at global row 0.
        _write_parquet(root / "data" / "chunk-000" / "file-000.parquet", _frames(3))
        _write_parquet(root / "data" / "chunk-000" / "file-001.parquet", _frames(2, offset=3))
        records = [
            _v3_record(0, 0, 0, 0, 3, length=3),
            _v3_record(1, 0, 1, 3, 5, length=2),
        ]
    else:
        _write_parquet(root / "data" / "chunk-000" / "file-000.parquet", _frames(6))
        records = [
            _v3_record(0, 0, 0, 0, 3, length=3),
            _v3_record(1, 0, 0, 3, 6, length=3),
        ]
    _write_parquet(
        root / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
        pa.table({name: [r[name] for r in records] for name in records[0]}),
    )
    return root


def _v3_record(
    episode: int, chunk: int, file_index: int, start: int, stop: int, *, length: int
) -> dict[str, Any]:
    return {
        "episode_index": episode,
        "data/chunk_index": chunk,
        "data/file_index": file_index,
        "dataset_from_index": start,
        "dataset_to_index": stop,
        "tasks": [f"task {episode}"],
        "length": length,
        "stats/action/min": [float(start)],
        "stats/action/max": [float(stop)],
        "stats/action/mean": [(start + stop) / 2],
        "stats/action/std": [1.0],
        "stats/action/count": [length],
    }


# ------------------------------------------------------------------ v3 behaviour


def test_sniff_only_requires_the_info_file(tmp_path: Path) -> None:
    reader = LeRobotReader()
    assert reader.sniff(tmp_path) is False
    _write_json(tmp_path / "meta" / "info.json", _info())
    assert reader.sniff(tmp_path) is True


def test_v3_slices_the_global_row_range_to_the_right_episode(tmp_path: Path) -> None:
    root = _v3_dataset(tmp_path / "ds")
    extraction = LeRobotReader().read(root, episode_key="episode_index=1")

    assert extraction.format == "lerobot-v3"
    assert extraction.format_version == "v3.0"
    assert extraction.frame_count == 3
    assert extraction.task == "task 1"
    # Global rows 3..6, so the timestamps run 3, 4, 5 rather than 0, 1, 2.
    assert extraction.duration_seconds == pytest.approx(2.0)


def test_v3_rebases_the_offset_when_episodes_span_files(tmp_path: Path) -> None:
    """A v3 row range is dataset-global; the file's starting row has to be removed."""
    root = _v3_dataset(tmp_path / "ds", two_files=True)
    reader = LeRobotReader()

    first = reader.read(root, episode_key="episode_index=0")
    second = reader.read(root, episode_key="episode_index=1")

    assert first.frame_count == 3
    assert second.frame_count == 2
    assert second.duration_seconds == pytest.approx(1.0)
    # The second file's own rows are 3 and 4, not 0 and 1: a naive slice would start
    # at global row 3 of a 2-row file and fail, or silently read the wrong episode.
    assert next(c for c in second.channels if c.name == "action").min == pytest.approx(3.0)


def test_v3_prefers_the_format_published_statistics(tmp_path: Path) -> None:
    root = _v3_dataset(tmp_path / "ds")
    action = next(
        c for c in LeRobotReader().read(root, episode_key="0").channels if c.name == "action"
    )
    assert action.min == pytest.approx(0.0)
    assert action.max == pytest.approx(3.0)
    assert action.count == 3


def test_v3_reports_features_the_data_file_does_not_carry(tmp_path: Path) -> None:
    root = _v3_dataset(tmp_path / "ds")
    record = {
        "episode_index": 0,
        "data/chunk_index": 0,
        "data/file_index": 0,
        "dataset_from_index": 0,
        "dataset_to_index": 3,
        "tasks": ["t"],
        "length": 3,
        "stats/observation.images.up/count": [100],
    }
    _write_parquet(
        root / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
        pa.table({name: [value] for name, value in record.items()}),
    )
    names = {c.name: c for c in LeRobotReader().read(root).channels}
    assert "action" in names
    assert names["observation.images.up"].count == 100
    assert names["observation.images.up"].dtype == "published"


def test_v3_structural_columns_are_not_reported_as_features(tmp_path: Path) -> None:
    names = {c.name for c in LeRobotReader().read(_v3_dataset(tmp_path / "ds")).channels}
    assert names == {"action"}


def test_quality_signals_are_computed_from_the_sliced_rows(tmp_path: Path) -> None:
    """Quality is summarized while the rows are in memory (ADR 0018)."""
    root = _v3_dataset(tmp_path / "ds")
    quality = LeRobotReader().read(root, episode_key="episode_index=1").quality

    assert quality is not None
    assert quality.frame_count == 3
    assert {dim.name for dim in quality.dims} == {"action[0]", "action[1]"}
    # Rows 3..6: action[0] steps 1.0 and action[1] steps 2.0 per transition.
    assert quality.movement_score == pytest.approx(5**0.5)
    assert quality.stall_ratio == 0.0
    # Three unique values per dim read as discrete, and discrete dims are not
    # judged; the verdict buckets are unit-tested on longer series in test_quality.
    assert quality.verdict == "unknown"


# ------------------------------------------------------------------ v2 behaviour


def test_v2_reads_one_file_per_episode_and_jsonl_index(tmp_path: Path) -> None:
    root = _v2_dataset(tmp_path / "ds")
    reader = LeRobotReader()

    assert reader.sniff(root)
    for episode in (0, 1):
        extraction = reader.read(root, episode_key=f"episode_index={episode}")
        assert extraction.format == "lerobot-v2"
        assert extraction.format_version == "v2.1"
        assert extraction.frame_count == 3
        assert extraction.task == f"task {episode}"
        assert extraction.source_path.name == f"episode_{episode:06d}.parquet"


def test_v2_and_v3_agree_on_the_shape_of_the_result(tmp_path: Path) -> None:
    """One reader, two layouts: the extraction contract must not fork on version."""
    v2 = LeRobotReader().read(_v2_dataset(tmp_path / "a"), episode_key="0")
    v3 = LeRobotReader().read(_v3_dataset(tmp_path / "b"), episode_key="0")
    assert v2.format != v3.format
    assert v2.metadata().keys() == v3.metadata().keys()
    assert v2.robot_type == v3.robot_type


def test_column_without_numbers_reports_no_statistics(tmp_path: Path) -> None:
    """A list-of-strings feature is real data, not an error."""
    root = _v2_dataset(tmp_path / "ds")
    _write_parquet(
        root / "data" / "chunk-000" / "episode_000000.parquet",
        pa.table({"task.instructions": ["go left", "go right", "stop"]}),
    )
    channel = next(
        c
        for c in LeRobotReader().read(root, episode_key="0").channels
        if c.name == "task.instructions"
    )
    assert channel.count == 0
    assert channel.mean is None
    assert channel.min is None


# ----------------------------------------------------------------- failure modes


def test_rejects_an_unknown_codebase_version(tmp_path: Path) -> None:
    root = _v2_dataset(tmp_path / "ds", version="v9.9")
    with pytest.raises(ReaderError, match="unsupported LeRobot codebase_version"):
        LeRobotReader().read(root)


def test_rejects_a_dataset_with_no_episode_index(tmp_path: Path) -> None:
    root = tmp_path / "ds"
    _write_json(root / "meta" / "info.json", _info())
    with pytest.raises(ReaderError, match="declares no episodes"):
        LeRobotReader().read(root)


def test_rejects_an_episode_that_is_not_in_the_dataset(tmp_path: Path) -> None:
    with pytest.raises(ReaderError, match="not in the dataset"):
        LeRobotReader().read(_v3_dataset(tmp_path / "ds"), episode_key="episode_index=99")


def test_rejects_a_missing_data_file(tmp_path: Path) -> None:
    root = _v3_dataset(tmp_path / "ds")
    (root / "data" / "chunk-000" / "file-000.parquet").unlink()
    with pytest.raises(ReaderError, match="data file is missing"):
        LeRobotReader().read(root)


def test_rejects_a_row_range_that_does_not_fit_the_file(tmp_path: Path) -> None:
    root = _v3_dataset(tmp_path / "ds")
    index_file = root / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    records = pq.read_table(index_file).to_pylist()
    records[0]["dataset_to_index"] = 9_999
    _write_parquet(index_file, pa.table({name: [r[name] for r in records] for name in records[0]}))
    with pytest.raises(ReaderError, match="which holds"):
        LeRobotReader().read(root, episode_key="0")


def test_rejects_malformed_episode_jsonl(tmp_path: Path) -> None:
    root = _v2_dataset(tmp_path / "ds")
    (root / "meta" / "episodes.jsonl").write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ReaderError, match="not valid JSON"):
        LeRobotReader().read(root)


def test_rejects_a_corrupt_parquet_file(tmp_path: Path) -> None:
    root = _v3_dataset(tmp_path / "ds")
    (root / "data" / "chunk-000" / "file-000.parquet").write_bytes(b"not parquet")
    with pytest.raises(ReaderError, match="cannot read Parquet"):
        LeRobotReader().read(root)


def test_rejects_a_directory_that_is_not_a_dataset(tmp_path: Path) -> None:
    with pytest.raises(ReaderError, match="not a LeRobot dataset"):
        LeRobotReader().read(tmp_path)


def test_registry_reports_the_formats_it_tried(tmp_path: Path) -> None:
    with pytest.raises(ReaderError, match="no reader recognises"):
        read_episode(tmp_path)
    assert reader_for(_v3_dataset(tmp_path / "ds")) is not None


# ------------------------------------------------------------------ real datasets


@pytest.mark.network
def test_real_v3_dataset_reads_the_published_so101_episode(
    real_lerobot_dataset: Callable[[str], Path],
) -> None:
    """Real artifact: lerobot/svla_so101_pickplace, LeRobot v3.0, SO-101 pick-place."""
    root = real_lerobot_dataset("v3")
    extraction = LeRobotReader().read(root, episode_key="episode_index=0")

    assert extraction.format == "lerobot-v3"
    assert extraction.format_version == "v3.0"
    assert extraction.robot_type == "so100_follower"
    assert extraction.fps == pytest.approx(30.0)
    assert extraction.task == "pink lego brick into the transparent box"
    assert extraction.frame_count == 303
    assert extraction.duration_seconds == pytest.approx(10.067, abs=0.01)
    assert extraction.dataset["total_episodes"] == 50
    assert extraction.dataset["has_video"] is True

    names = {c.name for c in extraction.channels}
    assert {"action", "observation.state"} <= names


@pytest.mark.network
def test_real_v3_episodes_have_distinct_lengths(
    real_lerobot_dataset: Callable[[str], Path],
) -> None:
    """Guards the row-range slicing against the worst bug: a constant full-file read."""
    root = real_lerobot_dataset("v3")
    reader = LeRobotReader()
    lengths = {
        episode: reader.read(root, episode_key=str(episode)).frame_count for episode in (0, 7, 49)
    }

    assert lengths[0] == 303
    assert len(set(lengths.values())) == 3, f"episodes must not all read the same rows: {lengths}"


@pytest.mark.network
def test_real_v2_dataset_reads_with_a_different_embodiment(
    real_lerobot_dataset: Callable[[str], Path],
) -> None:
    """Real artifact: yaak-ai/lerobot-driving-school, LeRobot v2.1."""
    root = real_lerobot_dataset("v2")
    extraction = LeRobotReader().read(root, episode_key="episode_index=0")

    assert extraction.format == "lerobot-v2"
    assert extraction.format_version == "v2.1"
    assert extraction.robot_type == "KIA Niro EV 2023"
    assert extraction.frame_count == 460
    assert extraction.task.startswith("Follow the waypoints")
    assert extraction.metadata()["channel_stats"]["action.continuous"]["count"] > 0
