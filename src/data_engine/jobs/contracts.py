"""Typed worker-stage contract; handlers are implemented in their owning modules."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from data_engine.jobs.state import JobType


@dataclass(frozen=True, slots=True)
class JobContext:
    job_id: str
    correlation_id: str
    cancel_requested: bool
    deadline: datetime | None


@dataclass(frozen=True, slots=True)
class StageResult:
    ok: bool
    outputs: Mapping[str, Any]
    reason_code: str | None = None


class StageHandler(Protocol):
    name: JobType

    def run(self, ctx: JobContext, payload: Mapping[str, Any]) -> StageResult: ...
