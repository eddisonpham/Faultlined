"""Request and response models for the task-vocabulary API (ADR 0029)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VocabularyEntry(BaseModel):
    """One curated activity."""

    id: str
    preferred_label: str
    core: str = ""
    notes: str = ""
    created_at: str | None = None
    created_by: str = ""
    task_count: int = 0
    episodes: int = 0


class VocabularyHealth(BaseModel):
    """The numbers the page leads with; unmapped share is the headline (ADR 0029 §9)."""

    entries: int = 0
    orphaned_mappings: int = 0
    pending_candidates: int = 0
    task_strings: int = 0
    mapped_strings: int = 0
    dismissed_strings: int = 0
    unmapped_strings: int = 0
    unmapped_string_share: float = 0.0
    episodes: int = 0
    mapped_episodes: int = 0
    dismissed_episodes: int = 0
    unmapped_episodes: int = 0
    unmapped_episode_share: float = 0.0


class VocabularyListResponse(BaseModel):
    entries: list[VocabularyEntry]
    health: VocabularyHealth


class VocabularyUnmappedRow(BaseModel):
    """One novel task string - the bounded review queue's unit."""

    task_string: str
    episodes: int
    first_seen: str | None = None


class QueueCursor(BaseModel):
    """Continuation position in the ranked queue: pass back as after_* params."""

    episodes: int
    task_string: str


class VocabularyUnmappedResponse(BaseModel):
    items: list[VocabularyUnmappedRow]
    next_after: QueueCursor | None = None


class VocabularyCandidate(BaseModel):
    """One ranked suggestion."""

    kind: Literal["attach", "new_entry"]
    core: str
    task_strings: list[str]
    episodes: int
    entry_id: str | None = None
    entry_label: str | None = None


class VocabularyCandidateListResponse(BaseModel):
    items: list[VocabularyCandidate]


class VocabularyMember(BaseModel):
    task_string: str
    provenance: str
    episodes: int = 0
    mapped_at: str | None = None


class VocabularyEntryDetailResponse(BaseModel):
    entry: VocabularyEntry
    members: list[VocabularyMember]


class VocabularyEntryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_label: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)


class VocabularyEntryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_label: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def at_least_one_field(self) -> VocabularyEntryUpdateRequest:
        if self.preferred_label is None and self.notes is None:
            raise ValueError("at least one field is required")
        return self


class VocabularyMapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_string: str = Field(min_length=1, max_length=1000)
    entry_id: str = Field(min_length=1, max_length=100)


class VocabularyDismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_string: str = Field(min_length=1, max_length=1000)


class VocabularyMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_entry_id: str = Field(min_length=1, max_length=100)


class VocabularySplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_strings: list[str] = Field(min_length=1, max_length=500)
    new_label: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def split_strings_are_unique(self) -> VocabularySplitRequest:
        if len(set(self.task_strings)) != len(self.task_strings):
            raise ValueError("task_strings must be unique")
        return self


class VocabularyMapping(BaseModel):
    task_string: str
    entry_id: str | None = None
    provenance: str


class VocabularyMergeResponse(BaseModel):
    event_id: int
    moved: int
    source: VocabularyEntry
    target_id: str


class VocabularySplitResponse(BaseModel):
    event_id: int
    moved: int
    entry_id: str
    new_entry: VocabularyEntry


class VocabularyEvent(BaseModel):
    id: int
    kind: str
    payload: dict[str, object]
    created_at: str | None = None
    created_by: str = ""
    undone_at: str | None = None


class VocabularyEventListResponse(BaseModel):
    items: list[VocabularyEvent]


class VocabularyEventUndoResponse(BaseModel):
    event_id: int
    kind: str
    undone: bool
