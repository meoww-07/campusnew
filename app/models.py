"""Pydantic models: DB rows, ingestion inputs, and LLM JSON outputs."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

SourceType = Literal["telegram", "whatsapp", "gmail", "upload"]
EventType = Literal["exam", "deadline", "event"]
EventStatus = Literal["upcoming", "past", "conflict", "cancelled", "done"]
JobStatus = Literal["pending", "processing", "done", "failed"]
Category = Literal["exam", "deadline", "event", "notice", "resource", "placement", "general"]
Importance = Literal["high", "medium", "low"]


# ---------- DB rows ----------

class User(BaseModel):
    id: int
    telegram_chat_id: int
    name: str | None = None
    timezone: str = "Asia/Kolkata"
    gmail_refresh_token: str | None = None
    gmail_last_sync: str | None = None
    ical_token: str | None = None
    last_seen_today_at: str | None = None
    created_at: str


class Source(BaseModel):
    id: int
    user_id: int
    type: SourceType
    external_id: str | None = None
    title: str | None = None
    file_path: str | None = None
    content_hash: str
    created_at: str


class Segment(BaseModel):
    id: int
    source_id: int
    user_id: int
    text: str
    locator: dict[str, Any] = Field(default_factory=dict)
    observed_at: str
    summary: str | None = None
    category: Category | None = None
    subject: str | None = None
    importance: Importance | None = None
    extracted_at: str | None = None
    opened_at: str | None = None


class Chunk(BaseModel):
    id: int
    segment_id: int
    user_id: int
    text: str
    char_start: int
    char_end: int


class Event(BaseModel):
    id: int
    user_id: int
    segment_id: int | None = None  # None for events the user added by hand
    title: str
    type: EventType
    subject: str | None = None
    due_at: str
    all_day: bool = False
    date_text: str | None = None
    confidence: float
    status: EventStatus = "upcoming"
    valid_until: str
    superseded_by: int | None = None  # set when a newer announcement replaced this date
    end_at: str | None = None
    location: str | None = None
    user_edited: bool = False  # never overwritten by re-extraction
    version: int = 1  # bumped when the date changes, so reminders are sent again
    created_at: str | None = None
    updated_at: str | None = None


class Job(BaseModel):
    id: int
    source_id: int | None = None
    space_id: int | None = None
    user_id: int
    kind: str
    status: JobStatus
    attempts: int
    last_error: str | None = None
    locked_at: str | None = None
    created_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------- Inputs ----------

class SegmentIn(BaseModel):
    text: str
    locator: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime


class ChunkIn(BaseModel):
    text: str
    char_start: int
    char_end: int


class EventIn(BaseModel):
    segment_id: int
    title: str
    type: EventType
    subject: str | None = None
    due_at: datetime  # must be timezone-aware
    all_day: bool = False
    date_text: str | None = None
    confidence: float = Field(ge=0, le=1)


SpaceKind = Literal["auto", "custom"]
AddedBy = Literal["auto", "rule", "user"]


class SpaceRules(BaseModel):
    """Custom space rules. All given conditions must hold (AND); list entries are OR'd."""
    model_config = {"extra": "forbid"}

    any_keywords: list[str] = Field(default_factory=list)
    all_keywords: list[str] = Field(default_factory=list)
    source_types: list[SourceType] = Field(default_factory=list)
    senders: list[str] = Field(default_factory=list)  # names, emails, or domains like "@college.edu.in"
    categories: list[Category] = Field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None
    similar_to: str | None = None
    similar_max_distance: float | None = Field(default=None, gt=0, le=2)

    @field_validator("any_keywords", "all_keywords", "senders", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> list[str]:
        return [s.strip() for s in (v or []) if isinstance(s, str) and s.strip()]

    def is_empty(self) -> bool:
        return not (self.any_keywords or self.all_keywords or self.source_types or self.senders
                    or self.categories or self.date_from or self.date_to or self.similar_to)


class Space(BaseModel):
    id: int
    user_id: int
    name: str
    kind: SpaceKind
    auto_key: str | None = None
    icon: str | None = None
    color: str | None = None
    rules: SpaceRules = Field(default_factory=SpaceRules)
    pinned: bool = False
    merged_into: int | None = None
    deleted_at: str | None = None
    last_activity_at: str | None = None
    created_at: str


class SegmentContext(BaseModel):
    """What rule matching needs to know about a segment."""
    segment_id: int
    text: str
    summary: str | None = None
    category: Category | None = None
    subject: str | None = None
    observed_at: str
    locator: dict[str, Any] = Field(default_factory=dict)
    source_type: SourceType
    source_title: str | None = None
    event_types: list[str] = Field(default_factory=list)
    event_subjects: list[str] = Field(default_factory=list)  # effective subject per event


class IngestResult(BaseModel):
    source_id: int
    created: bool  # False => duplicate content, nothing new stored
    job_id: int | None = None


class ChunkHit(BaseModel):
    """A chunk joined with its segment and source, for search results."""
    chunk_id: int
    text: str
    segment_id: int
    locator: dict[str, Any]
    observed_at: str
    source_id: int
    source_type: SourceType
    source_title: str | None = None
    category: Category | None = None
    created_at: str = ""  # source ingestion time


# ---------- LLM output (validated) ----------

class ExtractedEvent(BaseModel):
    title: str
    type: EventType
    subject: str | None = None
    date_text: str | None = None
    due_at: datetime | None = None
    all_day: bool = False
    confidence: float = Field(ge=0, le=1)


_CATEGORIES = {"exam", "deadline", "event", "notice", "resource", "placement", "general"}
_EVENT_TYPES = {"exam", "deadline", "event"}


class Extraction(BaseModel):
    """Lenient on purpose: one malformed event (or an off-list label) must not
    discard the whole extraction, so bad events are dropped and unknown
    labels fall back to safe defaults."""
    summary: str = ""
    category: Category = "general"
    subject: str | None = None
    importance: Importance = "medium"
    events: list[ExtractedEvent] = Field(default_factory=list)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v: Any) -> str:
        v = str(v or "").strip().lower()
        return v if v in _CATEGORIES else "general"

    @field_validator("importance", mode="before")
    @classmethod
    def _importance(cls, v: Any) -> str:
        v = str(v or "").strip().lower()
        return v if v in ("high", "medium", "low") else "medium"

    @field_validator("subject", "summary", mode="before")
    @classmethod
    def _text(cls, v: Any) -> Any:
        return None if v in ("", "null", "None") else v

    @field_validator("events", mode="before")
    @classmethod
    def _events(cls, v: Any) -> list[Any]:
        out = []
        for item in v if isinstance(v, list) else []:
            if isinstance(item, ExtractedEvent):
                out.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item = {**item, "type": str(item.get("type", "")).strip().lower()}
            if item.get("subject") in ("", "null", "None"):
                item["subject"] = None
            if item["type"] not in _EVENT_TYPES:
                item["type"] = "event"
            try:
                out.append(ExtractedEvent.model_validate(item))
            except ValidationError:
                continue  # e.g. unparseable due_at: drop just this event
        return out
