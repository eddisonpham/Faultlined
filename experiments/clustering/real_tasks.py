"""Harvest the task sentence from one LeRobot dataset on the Hub."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

REPOS: tuple[str, ...] = (
    "lerobot/pusht",
    "lerobot/pusht_keyboard",
    "lerobot/pusht_image",
    "lerobot/aloha_sim_insertion_human",
    "lerobot/aloha_sim_transfer_cube_human",
    "lerobot/aloha_sim_chat_insertion_human",
    "lerobot/aloha_sim_insertion_human_ruyu",
    "lerobot/aloha_static_bread_pick_and_place",
    "lerobot/aloha_static_teddy_bear_pick_and_place",
    "lerobot/aloha_static_ziploc_pick_and_place",
    "lerobot/aloha_static_coffee_pod",
    "lerobot/aloha_static_hanging_shirt",
    "lerobot/aloha_static_tote_bag",
    "lerobot/xarm_lift_medium",
    "lerobot/xarm_lift_small",
    "lerobot/xarm_lift_large",
    "lerobot/xarm_push_medium",
    "lerobot/xarm_sponges",
    "lerobot/xarm_transfer_blocks",
    "lerobot/xarm_place_medium",
    "lerobot/xarm_place_small",
    "lerobot/xarm_place_large",
    "lerobot/libero",
    "lerobot/libero_10",
    "physical-intelligence/libero",
    "physical-intelligence/libero_10",
    "physical-intelligence/libero_spatial",
    "physical-intelligence/libero_object",
    "physical-intelligence/libero_goal",
    "physical-intelligence/libero_90",
    "lerobot/so101_pickplace",
    "lerobot/so101_stacking",
    "lerobot/so101_pick_place",
    "lerobot/svla_so101_pickplace",
    "lerobot/svla_so101_stacking",
    "lerobot/driving_school",
    "lerobot/furniture_bench",
    "lerobot/robohouse",
    "lerobot/open_loop_human",
    "lerobot/human_grasping",
    "lerobot/ucsd_pick_and_place_dataset_v2",
    "lerobot/bridge_orig_lerobot",
    "lerobot/asim",
    "IPEC-COMMUNITY/panoptic_take_three",
    "IPEC-COMMUNITY/panoptic_take_two",
    "lerobot/nyu_bddx",
    "lerobot/dROID",
)

LAYOUTS: tuple[str, ...] = ("meta/tasks.parquet", "meta/tasks.jsonl", "meta/tasks.csv")

CACHE = Path("experiments/clustering/results/real_tasks.json")

ATTEMPTS = 2
BACKOFF_SECONDS = 1.5


@dataclass(frozen=True, slots=True)
class Harvest:
    """What came back, and what did not."""

    rows: tuple[tuple[str, str], ...]
    resolved: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]

    @property
    def count(self) -> int:
        return len(self.rows)


def _texts_from(path: Path) -> list[str]:
    """Read one task file, whatever shape LeRobot wrote it in."""
    if path.suffix == ".parquet":
        import pyarrow.parquet as pq

        table = pq.read_table(path)
        column = "__index_level_0__" if "__index_level_0__" in table.column_names else None
        if column is None:
            column = "task" if "task" in table.column_names else table.column_names[0]
        return [str(value) for value in table.column(column).to_pylist()]
    if path.suffix == ".jsonl":
        out: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            doc = json.loads(line)
            text = doc.get("task")
            if isinstance(text, str):
                out.append(text)
        return out
    import csv as _csv

    with path.open(encoding="utf-8", newline="") as handle:
        return [row.get("task", "") for row in _csv.DictReader(handle)]


def _usable(text: str) -> bool:
    """Reject placeholders."""
    text = text.strip()
    return bool(text) and not text.isdigit() and len(text) > 3


def _is_repo_level(entry: str) -> bool:
    """True when the failure is about the repository, not about one file in it."""
    return any(marker in entry for marker in ("RepositoryNotFound", "GatedRepo", "401"))


def _best_reason(seen: Sequence[str]) -> str:
    """The most explanatory failure, so a report can say why coverage is thin."""
    for marker in ("RepositoryNotFound", "401", "GatedRepo"):
        for entry in seen:
            if marker in entry:
                return entry
    return seen[-1] if seen else "no task file"


def harvest(
    repos: tuple[str, ...] = REPOS, *, cache: Path | None = CACHE, refresh: bool = False
) -> Harvest:
    """Read one task sentence per dataset, from cache unless asked to refresh."""
    if cache is not None and cache.exists() and not refresh:
        doc = json.loads(cache.read_text(encoding="utf-8"))
        return Harvest(
            rows=tuple((row[0], row[1]) for row in doc["rows"]),
            resolved=tuple(doc["resolved"]),
            failed=tuple((row[0], row[1]) for row in doc["failed"]),
        )

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        return Harvest(rows=(), resolved=(), failed=(("huggingface_hub", "not installed"),))

    rows: list[tuple[str, str]] = []
    resolved: list[str] = []
    failed: list[tuple[str, str]] = []
    for repo in repos:
        seen: list[str] = []
        found = False
        for filename in LAYOUTS:
            path: Path | None = None
            for attempt in range(ATTEMPTS):
                try:
                    path = Path(hf_hub_download(repo, filename, repo_type="dataset"))
                    break
                except Exception as error:
                    seen.append(f"{filename}: {type(error).__name__}")
                    if attempt + 1 < ATTEMPTS:
                        time.sleep(BACKOFF_SECONDS * (attempt + 1))
            if any(_is_repo_level(entry) for entry in seen):
                break
            if path is None:
                continue
            try:
                texts = [text for text in _texts_from(path) if _usable(text)]
            except Exception as error:
                failed.append((repo, f"{filename}: unreadable {type(error).__name__}"))
                found = True
                break
            resolved.append(repo)
            rows.extend((repo, text.strip()) for text in texts)
            found = True
            break
        if not found:
            failed.append((repo, _best_reason(seen)))

    unique = list(dict.fromkeys(rows))
    result = Harvest(rows=tuple(unique), resolved=tuple(resolved), failed=tuple(failed))
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "note": "task sentences harvested from the Hugging Face Hub; "
                    "regenerate with `just cluster scale --refresh-real`",
                    "rows": [list(row) for row in result.rows],
                    "resolved": list(result.resolved),
                    "failed": [list(row) for row in result.failed],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return result
