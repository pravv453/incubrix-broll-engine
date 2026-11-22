"""Public request schema for POST /api/v1/broll/jobs (matches the brief's example body)."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.core.errors import AppError, ErrorCode

AspectRatio = Literal["16:9", "9:16", "1:1"]
SUPPORTED_RATIOS: tuple[str, ...] = ("16:9", "9:16", "1:1")


class CaptionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    burn_in: bool = True
    srt: bool = True


class InputScene(BaseModel):
    """One entry of an IncuBrix scene list."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    visual_hint: str | None = Field(default=None, max_length=500)


class BrollJobOptions(BaseModel):
    """Everything except the primary input. Used as the multipart `options` field."""

    model_config = ConfigDict(extra="forbid")
    voice: str = Field(default="en-IN-female", max_length=64)
    language: str | None = Field(
        default=None, max_length=16, description="BCP-47 hint, e.g. en, hi, ta. Auto-detected when null."
    )
    aspect_ratios: list[str] = Field(default_factory=lambda: ["16:9"], min_length=1, max_length=3)
    style: str = Field(default="bright, modern, real-world footage", max_length=300)
    avoid: list[str] = Field(default_factory=list, max_length=20)
    captions: CaptionOptions = Field(default_factory=CaptionOptions)
    callback_url: HttpUrl | None = None

    @field_validator("aspect_ratios")
    @classmethod
    def _ratios(cls, v: list[str]) -> list[str]:
        bad = [r for r in v if r not in SUPPORTED_RATIOS]
        if bad:
            raise AppError(
                ErrorCode.INVALID_ASPECT_RATIO,
                details={"received": bad, "supported": list(SUPPORTED_RATIOS)},
            )
        # de-duplicate, keep order
        return list(dict.fromkeys(v))

    @field_validator("avoid")
    @classmethod
    def _avoid(cls, v: list[str]) -> list[str]:
        return [a.strip() for a in v if a and a.strip()]


class BrollJobRequest(BrollJobOptions):
    """JSON body: exactly one of audio_url / script / scenes."""

    audio_url: HttpUrl | None = None
    script: str | None = None
    scenes: list[InputScene] | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _exactly_one_input(self) -> "BrollJobRequest":
        provided = [
            name
            for name, val in (("audio_url", self.audio_url), ("script", self.script), ("scenes", self.scenes))
            if val is not None
        ]
        if not provided:
            raise AppError(ErrorCode.MISSING_INPUT)
        if len(provided) > 1:
            raise AppError(ErrorCode.MULTIPLE_INPUTS, details={"provided": provided})
        if self.script is not None and not self.script.strip():
            raise AppError(ErrorCode.EMPTY_SCRIPT)
        if self.scenes is not None:
            if not self.scenes or all(not s.text.strip() for s in self.scenes):
                raise AppError(ErrorCode.EMPTY_SCRIPT, details={"input": "scenes"})
        return self

    @property
    def input_kind(self) -> Literal["audio_url", "script", "scenes"]:
        if self.audio_url is not None:
            return "audio_url"
        if self.script is not None:
            return "script"
        return "scenes"
