from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.errors import AppError, ErrorCode

JobStatusLit = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class JobCreated(BaseModel):
    job_id: str
    status: JobStatusLit
    progress: int
    status_url: str
    created_at: datetime | None = None


class ResultLinks(BaseModel):
    videos: dict[str, str] = Field(default_factory=dict, description="aspect ratio -> download link")
    captions_srt: str | None = None
    timeline_json: str | None = None
    manifest_json: str | None = None


class JobStatus(BaseModel):
    job_id: str
    kind: str
    parent_job_id: str | None = None
    status: JobStatusLit
    stage: str
    progress: int = Field(ge=0, le=100)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    warnings: list[dict] = Field(default_factory=list)
    error: ErrorBody | None = None
    result: ResultLinks | None = None
    timeline_url: str | None = None


class SwapClipRequest(BaseModel):
    """Replace one scene's clip. Give at most one of asset_id, candidate_rank or query;
    with none, the next-best eligible clip is chosen automatically."""

    model_config = ConfigDict(extra="forbid")
    provider: str | None = Field(default=None, description="Required with asset_id, e.g. 'pixabay' or 'mock'")
    asset_id: str | None = Field(default=None, max_length=64)
    candidate_rank: int | None = Field(default=None, ge=1, le=10, description="Rank in the stored candidate list")
    query: str | None = Field(default=None, min_length=2, max_length=100, description="New search brief for this scene")
    reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _one(self) -> "SwapClipRequest":
        chosen = [k for k in ("asset_id", "candidate_rank", "query") if getattr(self, k) is not None]
        if len(chosen) > 1:
            raise AppError(ErrorCode.MULTIPLE_INPUTS, details={"provided": chosen})
        if self.asset_id and not self.provider:
            raise AppError(ErrorCode.INVALID_REQUEST, message="provider is required with asset_id")
        return self
