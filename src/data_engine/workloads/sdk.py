"""Public workload SDK: models are workloads and see only this boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class DatasetView:
    build_hash: str
    root: str


@dataclass(frozen=True, slots=True)
class WorkloadResult:
    artifacts: tuple[str, ...]
    metrics: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any]


class Workload(Protocol):
    name: str
    version: str
    resource: Literal["cpu", "gpu_optional", "gpu_required"]

    def run(self, dataset: DatasetView, config: Mapping[str, Any]) -> WorkloadResult: ...
