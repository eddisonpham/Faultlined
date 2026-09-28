"""Typed benchmark result schema v1 and mandatory provenance validation."""

from __future__ import annotations

import math
from datetime import datetime
from statistics import mean, stdev
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BenchmarkIdentity(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class GPUInfo(StrictModel):
    model: str = Field(min_length=1)
    memory_total_bytes: int = Field(gt=0)
    driver: str = Field(min_length=1)


class HardwareProvenance(StrictModel):
    cpu: str = Field(min_length=1)
    logical_cpus: int = Field(ge=1)
    ram_bytes: int = Field(gt=0)
    gpu: GPUInfo | None
    disk: str = Field(min_length=1)


class SoftwareProvenance(StrictModel):
    os: str = Field(min_length=1)
    python: str = Field(min_length=1)
    lockfile_hash: str = Field(min_length=1)
    app_version: str = Field(min_length=1)


class Provenance(StrictModel):
    git_commit: str = Field(min_length=1)
    git_dirty: bool
    config_hash: str = Field(min_length=1)
    dataset: str | None
    dataset_version: str | None
    model: str | None
    model_version: str | None
    hardware: HardwareProvenance
    software: SoftwareProvenance
    seeds: list[int]
    timestamp_utc: datetime
    workload: str = Field(min_length=1)
    workload_version: str = Field(min_length=1)
    system_parameters: dict[str, Any]
    background_load_note: str
    container_image_digest: str | None


class Trial(StrictModel):
    index: int = Field(ge=0)
    latency_seconds: float = Field(ge=0)
    success: bool
    resource_sample_start: int = Field(ge=0)
    resource_sample_end: int = Field(ge=0)


class ResultSummary(StrictModel):
    n: int = Field(ge=1)
    warmup_count: int = Field(ge=0)
    p50_seconds: float = Field(ge=0)
    p95_seconds: float = Field(ge=0)
    p99_seconds: float = Field(ge=0)
    mean_seconds: float = Field(ge=0)
    stddev_seconds: float = Field(ge=0)
    ci95_mean_seconds: tuple[float, float]
    failure_count: int = Field(ge=0)


class BenchmarkFailure(StrictModel):
    type: str = Field(min_length=1)
    message: str = Field(min_length=1)


class BenchmarkResult(StrictModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    benchmark: BenchmarkIdentity
    status: Literal["ok", "failed"]
    started_at: datetime
    duration_seconds: float = Field(ge=0)
    config: dict[str, Any]
    provenance: Provenance
    trials: list[Trial] = Field(min_length=1)
    summary: ResultSummary
    resource_samples: list[dict[str, Any]]
    failure: BenchmarkFailure | None

    @model_validator(mode="after")
    def summary_matches_trials(self) -> BenchmarkResult:
        failed_trials = sum(not trial.success for trial in self.trials)
        if self.summary.n != len(self.trials):
            raise ValueError("summary.n must match the number of raw trials")
        if self.summary.failure_count != failed_trials:
            raise ValueError("summary.failure_count must match trial outcomes")
        if [trial.index for trial in self.trials] != list(range(len(self.trials))):
            raise ValueError("trial indices must be contiguous and zero-based")
        for trial in self.trials:
            if trial.resource_sample_start > trial.resource_sample_end:
                raise ValueError("trial resource sample range must be ordered")
            if trial.resource_sample_end >= len(self.resource_samples):
                raise ValueError("trial resource sample reference is out of range")
        if self.status == "ok" and (self.failure is not None or failed_trials):
            raise ValueError("successful result cannot contain failed trials or failure details")
        if self.status == "failed" and (self.failure is None or failed_trials == 0):
            raise ValueError("failed result must contain failure details and a failed trial")
        latencies = [trial.latency_seconds for trial in self.trials]
        ordered = sorted(latencies)
        for field, percentile in (
            ("p50_seconds", 0.50),
            ("p95_seconds", 0.95),
            ("p99_seconds", 0.99),
        ):
            expected = ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]
            if getattr(self.summary, field) != expected:
                raise ValueError(f"summary.{field} must match raw trial latencies")
        if self.summary.mean_seconds != mean(latencies):
            raise ValueError("summary.mean_seconds must match raw trial latencies")
        expected_stddev = stdev(latencies) if len(latencies) > 1 else 0.0
        if self.summary.stddev_seconds != expected_stddev:
            raise ValueError("summary.stddev_seconds must match raw trial latencies")
        return self
