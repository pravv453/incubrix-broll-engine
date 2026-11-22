"""Transcript contract produced by every ASR adapter. ASR/alignment is the only
source of timing truth - no LLM ever writes a timestamp."""
from pydantic import BaseModel, Field, model_validator


class Word(BaseModel):
    text: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    confidence: float | None = None
    speaker_id: str | None = None

    @model_validator(mode="after")
    def _order(self) -> "Word":
        if self.end_s < self.start_s:
            raise ValueError(f"word '{self.text}' ends before it starts")
        return self


class Transcript(BaseModel):
    language: str | None = None
    duration_s: float = Field(gt=0, description="Duration of the narration audio, not of the last word")
    words: list[Word]
    provider: str = "unknown"
    model: str | None = None
    timing_source: str = Field(
        default="asr_word_timestamps",
        description="asr_word_timestamps | forced_alignment | segment_interpolated (low confidence)",
    )

    @model_validator(mode="after")
    def _monotonic(self) -> "Transcript":
        prev_start = -1.0
        for w in self.words:
            if w.start_s < prev_start:
                raise ValueError("word start times are not monotonic")
            if w.end_s > self.duration_s + 0.5:
                raise ValueError(f"word '{w.text}' ends after the audio ({w.end_s} > {self.duration_s})")
            prev_start = w.start_s
        return self

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)
