"""Normalised footage candidate + rights record. Provider responses are untrusted
input; they are mapped into this shape and then judged by the rights gate."""
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class Licence(BaseModel):
    name: str | None = None
    url: str | None = None
    commercial_use_allowed: bool = False
    attribution_required: bool = False
    evidence_status: str = "unverified"  # provider_policy_v1 | ai_generated_owned | unverified
    restrictions: list[str] = Field(default_factory=list)


class Attribution(BaseModel):
    required: bool = False
    text: str | None = None
    creator: str | None = None
    url: str | None = None


class FootageCandidate(BaseModel):
    provider: str
    asset_id: str
    kind: Literal["video", "generated"] = "video"
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    source_url: str | None = None  # human-facing page
    download_url: str | None = None
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    fps: float | None = None
    licence: Licence = Field(default_factory=Licence)
    attribution: Attribution = Field(default_factory=Attribution)
    is_ai_generated: bool = False
    watermarked: bool | None = None  # None = provider does not say
    editorial_only: bool = False
    retrieved_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    query: str = ""
    provider_rank: int = 0
    extra: dict = Field(default_factory=dict)  # provider-specific (e.g. mock pattern, generation prompt)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.asset_id}"

    @property
    def text_for_embedding(self) -> str:
        return " ".join(x for x in [self.title, ", ".join(self.tags), self.description] if x)


class ScoredCandidate(BaseModel):
    candidate: FootageCandidate
    score: float
    components: dict[str, float | None]
    rejected: bool = False
    reject_reasons: list[str] = Field(default_factory=list)
    rank: int | None = None
