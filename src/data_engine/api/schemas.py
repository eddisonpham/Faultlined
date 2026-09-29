"""HTTP request/response models for the vertical-slice public API."""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from data_engine.jobs.state import DEFAULT_MAX_ATTEMPTS


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


class SourceIngestPayload(BaseModel):
    """A real episode on disk, described by path rather than by body."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=1024)
    """Path to a dataset directory or file. Resolved by a reader via sniffing."""

    episode_key: str | None = Field(default=None, max_length=128)
    """Which episode inside the source, e.g. `episode_index=7`. First one if omitted."""


class _JobPolicy(BaseModel):
    """Retry/deadline policy shared by every job submission (ADR 0015)."""

    max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=10)
    """Retry budget (F6): how many times the worker may try before parking the job."""

    deadline_seconds: float | None = Field(default=None, gt=0)
    """Wall-clock budget from submission (F6); the reaper times the job out after it."""


class SubmitJobRequest(_JobPolicy):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ingest"]
    payload: IngestPayload


class SubmitSourceJobRequest(_JobPolicy):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ingest_source"]
    payload: SourceIngestPayload


AnyJobRequest = SubmitJobRequest | SubmitSourceJobRequest


class JobResponse(BaseModel):
    id: str
    type: str
    state: str
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    correlation_id: str
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    deadline_at: Any = None
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
    episode_key: str = ""
    artifact_hash: str
    format: str
    metadata: dict[str, Any]
    lineage: list[LineageEdgeResponse] = Field(default_factory=list)
    created_at: Any


class EpisodeSummary(BaseModel):
    """Row in the episodes catalog; quality columns are null pre-analysis."""

    id: str
    episode_key: str = ""
    format: str
    state: str
    created_at: Any
    frame_count: int | None = None
    movement_score: float | None = None
    jerk_score: float | None = None
    stall_ratio: float | None = None
    verdict: str | None = None


class EpisodeListResponse(BaseModel):
    items: list[EpisodeSummary]


class ValidationResultResponse(BaseModel):
    """One profile's verdict against one episode (ADR 0016)."""

    profile_hash: str
    profile_name: str
    profile_version: str
    passed: bool
    reason_codes: list[str] = Field(default_factory=list)
    violations: list[dict[str, Any]] = Field(default_factory=list)
    created_at: Any


class EpisodeValidationResponse(BaseModel):
    episode_id: str
    results: list[ValidationResultResponse]


class JobSummary(BaseModel):
    """Row in the jobs list; the full payload is available on the detail endpoint."""

    id: str
    type: str
    state: str
    correlation_id: str
    error: dict[str, Any] | None = None
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    created_at: Any
    started_at: Any = None
    finished_at: Any = None


class JobListResponse(BaseModel):
    items: list[JobSummary]
    next_before: Any | None = None


class JobReportEpisodes(BaseModel):
    """State and motion-quality rollup of the episodes one run produced."""

    total: int
    by_state: dict[str, int]
    verdicts: dict[str, int]
    flags: dict[str, int]
    length: dict[str, Any]
    mean_movement_score: float
    mean_jerk_score: float
    mean_stall_ratio: float


class JobReportValidation(BaseModel):
    """Validation verdicts over the episodes one run produced."""

    episodes_evaluated: int
    results: int
    passed: int
    failed: int
    reason_codes: dict[str, int]


class JobReportResponse(BaseModel):
    """Run triage card: what a job produced, how it validates, how it moves."""

    job: JobSummary
    episodes: JobReportEpisodes
    validation: JobReportValidation


class ArtifactSummary(BaseModel):
    hash: str
    size_bytes: int
    created_at: Any
    episode_ids: list[str] = Field(default_factory=list)


class ArtifactListResponse(BaseModel):
    items: list[ArtifactSummary]
    next_before: Any | None = None


class ResourceSample(BaseModel):
    cpu_percent: float | None = None
    memory_used_bytes: int | None = None
    memory_available_bytes: int | None = None
    disk_free_bytes: int | None = None
    gpu_present: bool = False
    gpu_memory_total_bytes: int | None = None


class StatusResponse(BaseModel):
    """Backs the Status page: health, queue depth, and host telemetry."""

    status: str
    queue_depth: dict[str, int]
    artifact_count: int
    episode_count: int
    resources: ResourceSample


class EpisodeQualityResponse(BaseModel):
    """Motion-quality signals for one episode (ADR 0018)."""

    episode_id: str
    frame_count: int
    movement_score: float
    jerk_score: float
    stall_ratio: float
    verdict: str
    dims: list[dict[str, Any]]
    length_zscore: float = 0.0
    computed_at: datetime | None = None


class QualitySummaryResponse(BaseModel):
    """Dataset-level distributions and outlier lists for curation triage."""

    episode_count: int
    verdicts: dict[str, int]
    length: dict[str, Any]
    speed_distribution: list[dict[str, Any]]
    heat_matrix: dict[str, Any]
    outliers: dict[str, list[dict[str, Any]]]


class MetricSummaryModel(BaseModel):
    """Descriptive statistics for one (metric, labels) sample set (ADR 0017)."""

    name: str
    labels: dict[str, str]
    label_key: str
    unit: str
    count: int
    min: float
    max: float
    mean: float
    sum: float
    p50: float
    p95: float
    p99: float
    first_timestamp: str | None = None
    last_timestamp: str | None = None


class MetricSeriesPoint(BaseModel):
    """One sparkline point: bucketed mean across the metric's label sets."""

    t: str
    v: float
    count: int


class MetricsResponse(BaseModel):
    """Aggregated runtime telemetry read back from the JSONL metrics sink."""

    generated_at: str
    window_seconds: float | None = None
    record_count: int
    summaries: list[MetricSummaryModel]
    series: dict[str, list[MetricSeriesPoint]]
    jobs_queue_depth: dict[str, int]
    worker_heartbeat_age_seconds: float | None = None
