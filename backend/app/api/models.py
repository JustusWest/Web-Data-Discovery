from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


RunState = Literal[
    "idle",
    "starting",
    "running",
    "paused",
    "awaiting_review",
    "stopping",
    "completed",
    "failed",
]


class CrawlExample(BaseModel):
    type: Literal["file", "url"]
    label: str | None = None
    url: str | None = None


class StartCrawlRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    maxDepth: int = Field(default=3, ge=1, le=10)
    minRelevance: float = Field(default=0.75, ge=0.0, le=1.0)
    domainFilter: str = ""
    examples: list[CrawlExample] = Field(default_factory=list)
    reviewBeforeCrawl: bool = False
    reviewPageCount: int = Field(default=3, ge=1, le=10)


class StartCrawlResponse(BaseModel):
    sessionId: str
    status: RunState


class StopCrawlResponse(BaseModel):
    sessionId: str
    status: RunState


class FeedbackRequest(BaseModel):
    resultId: str
    feedback: Literal["yes", "no"] | None = None
    notes: str = ""


class FeedbackResponse(BaseModel):
    updated: bool


class SummaryRequest(BaseModel):
    sampleSize: int = Field(default=40, ge=5, le=100)
    includeQueryHistory: bool = True


class SummaryResponse(BaseModel):
    sessionId: str
    status: RunState
    generatedAt: str
    sampleSize: int
    queryHistory: list[str]
    stats: dict
    summaryText: str


class ExportInfoResponse(BaseModel):
    sessionId: str
    filename: str


class SessionInfoResponse(BaseModel):
    sessionId: str
    status: RunState
    stats: dict
    startedAt: str | None
    completedAt: str | None
    errorMessage: str | None
