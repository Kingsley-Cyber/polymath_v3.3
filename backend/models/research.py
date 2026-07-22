"""Research job and artifact API models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


ResearchMode = Literal["quick", "standard", "deep"]
ResearchJobStatus = Literal[
    "queued",
    "running",
    "waiting_for_input",
    "rendering",
    "done",
    "failed",
    "cancelled",
]
ResearchArtifactFormat = Literal["markdown", "html", "pdf", "json"]


class ResearchBudgets(BaseModel):
    max_subquestions: int = Field(default=5, ge=1, le=32)
    max_tool_calls: int = Field(default=24, ge=1, le=200)
    max_graph_hops: int = Field(default=2, ge=0, le=4)
    max_evidence_items: int = Field(default=90, ge=1, le=500)
    max_output_tokens: int = Field(default=12000, ge=512, le=64000)


class ResearchJobCreate(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    corpus_ids: list[str] = Field(default_factory=list)
    mode: ResearchMode = "standard"
    budgets: ResearchBudgets | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchJob(BaseModel):
    job_id: str
    user_id: str
    status: ResearchJobStatus
    question: str
    corpus_ids: list[str]
    mode: ResearchMode
    budgets: ResearchBudgets
    artifact_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ResearchJobListResponse(BaseModel):
    items: list[ResearchJob]
    count: int


class ResearchArtifact(BaseModel):
    artifact_id: str
    job_id: str
    user_id: str
    format: ResearchArtifactFormat
    filename: str
    mime_type: str
    storage_path: str
    sha256: str
    size_bytes: int
    created_at: datetime


class ResearchArtifactListResponse(BaseModel):
    items: list[ResearchArtifact]
    count: int


class ResearchTraceEvent(BaseModel):
    event_id: str
    job_id: str
    user_id: str
    stage: str
    status: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ResearchTraceEventListResponse(BaseModel):
    items: list[ResearchTraceEvent]
    count: int
