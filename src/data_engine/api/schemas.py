"""HTTP request/response models for the vertical-slice public API."""

from __future__ import annotations

from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SyntheticEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=200)
    robot: str = Field(min_length=1, max_length=200)
    timestamps: list[float] = Field(min_length=1, max_length=100_000)
    observations: list[list[float]]
    actions: list[list[float]]

    @model_validator(mode="after")
    def aligned_samples(self) -> SyntheticEpisode:
        count = len(self.timestamps)
        if len(self.observations) != count or len(self.actions) != count:
            raise ValueError("timestamps, observations, and actions must have equal lengths")
        if any(right < left for left, right in pairwise(self.timestamps)):
            raise ValueError("timestamps must be monotonic non-decreasing")
        return self


class IngestPayload(BaseModel):
    episode: SyntheticEpisode


class SubmitJobRequest(BaseModel):
    type: Literal["ingest"]
    payload: IngestPayload


class JobResponse(BaseModel):
    id: str
    type: str
    state: str
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    correlation_id: str
    created_at: Any
    started_at: Any = None
    finished_at: Any = None


class LineageEdgeResponse(BaseModel):
    from_type: str
    from_ref: str
    to_type: str
    to_ref: str
    relation: str


class EpisodeResponse(BaseModel):
    id: str
    source_hash: str
    artifact_hash: str
    format: str
    metadata: dict[str, Any]
    lineage: list[LineageEdgeResponse] = Field(default_factory=list)
    created_at: Any
