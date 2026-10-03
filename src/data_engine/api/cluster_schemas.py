"""Cluster proposal schemas (ADR 0026).

Small and explicit rather than permissive: a rebuild takes three decisions (which axes to
ignore, how wide the radius is, which source to read) and each one changes what the
numbers mean, so each is a field with a documented default instead of a bag of options.

`ClusterProposal.model_config` forbids extra fields. A typo in an ignored axis would
otherwise be accepted silently and produce a clustering that ignored nothing - the sort
of failure that looks like a result.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from data_engine.clustering.lexicon import AXES


class ClusterRebuildRequest(BaseModel):
    """Recompute proposals. Every field changes what the numbers mean, so none is free."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["catalog", "sample"] = Field(
        default="catalog",
        description=(
            "`catalog` clusters the distinct task strings the catalog holds. `sample` "
            "clusters the shipped LeRobot sentences instead, which is illustrative data "
            "and is labelled as such."
        ),
    )
    ignore: list[Literal["verb", "colour", "site"]] = Field(
        default_factory=list,
        description=(
            "Axes to remove before grouping. Removing too little fragments a task across "
            "clusters; removing too much merges unrelated tasks. Defaults to none."
        ),
    )
    radius: float = Field(
        default=0.30,
        ge=0.0,
        le=2.0,
        description="Cosine distance within which a core joins the nearest cluster.",
    )
    rule: Literal["running_mean", "sliding_8", "sliding_32", "ema_0.98"] = Field(
        default="running_mean", description="How a cluster centroid moves as members arrive."
    )
    limit: int = Field(
        default=5000, ge=1, le=50_000, description="Maximum distinct task strings to read."
    )

    def ignored_argument(self) -> str:
        """The comma form `Ignored.parse` takes. Kept here so both paths agree."""
        allowed = set(AXES)
        return ",".join(sorted(axis for axis in self.ignore if axis in allowed))


class ClusterConfirmRequest(BaseModel):
    """Confirm a proposal: give it a name, which also freezes it."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=120, description="Human name for the group.")

    @field_validator("label")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """`min_length=1` accepts "  ", and a confirmation called "  " names nothing.

        Recorded labels are read back on the page and used to explain a frozen cluster,
        so an empty-looking one is worse than a rejected request.
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("a confirmation needs a label")
        return cleaned


class ClusterMember(BaseModel):
    task: str
    core: str
    episodes: int
    verb: str = ""
    colours: list[str] = Field(default_factory=list)
    distance: float = 0.0


class ClusterReviewDecision(BaseModel):
    task: str
    disposition: Literal["class", "dismissed"]
    label: str = ""


class ClusterReviewCandidate(BaseModel):
    task: str
    core: str
    cluster_key: str
    proposal_core: str
    episodes: int
    distance: float
    radius: float
    reason: str
    source: str


class ClusterReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[str] = Field(min_length=1, max_length=25)
    disposition: Literal["class", "dismissed"]
    label: str = Field(default="", max_length=120)

    @field_validator("tasks")
    @classmethod
    def _distinct_nonblank_tasks(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not cleaned:
            raise ValueError("select at least one task string")
        return cleaned

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _required_for_class(self) -> ClusterReviewRequest:
        if self.disposition == "class" and not self.label:
            raise ValueError("a human class needs a label")
        return self


class ClusterProposal(BaseModel):
    """One proposal. `frozen` is derived from a confirmation existing, never stored twice."""

    key: str
    core: str
    label: str = ""
    frozen: bool = False
    episodes: int = 0
    task_count: int = 0
    merged_cores: list[str] = Field(default_factory=list)
    verbs: dict[str, int] = Field(default_factory=dict)
    colours: dict[str, int] = Field(default_factory=dict)


class ClusterRun(BaseModel):
    """One rebuild, with the health numbers that say whether to trust it."""

    id: int = 0
    source: str = "catalog"
    ignored: str = ""
    radius: float = 0.30
    rule: str = "running_mean"
    created_at: str | None = None
    health: dict[str, object] = Field(default_factory=dict)


class ClusterReviewListResponse(BaseModel):
    candidates: list[ClusterReviewCandidate] = Field(default_factory=list)
    decisions: list[ClusterReviewDecision] = Field(default_factory=list)


class ClusterReviewDecisionResponse(BaseModel):
    reviewed: int
    decisions: list[ClusterReviewDecision] = Field(default_factory=list)


class ClusterReviewUndoResponse(BaseModel):
    reopened: int


class ClusterListResponse(BaseModel):
    proposals: list[ClusterProposal]
    run: ClusterRun | None = None
    health: dict[str, object] = Field(default_factory=dict)
    review_candidates: list[ClusterReviewCandidate] = Field(default_factory=list)
    review_decisions: list[ClusterReviewDecision] = Field(default_factory=list)


class ClusterDetailResponse(BaseModel):
    proposal: ClusterProposal
    members: list[ClusterMember]


class ClusterHealthResponse(BaseModel):
    """The numbers the page leads with, without the proposals."""

    health: dict[str, object]
    run: ClusterRun | None = None
