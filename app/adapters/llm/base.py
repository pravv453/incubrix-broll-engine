from dataclasses import dataclass
from typing import Protocol

from app.schemas.scenes import ScenePlan


@dataclass
class SpanInfo:
    index: int
    text: str
    duration_s: float


@dataclass
class PlanRequest:
    spans: list[SpanInfo]
    style: str
    avoid: list[str]
    language: str | None
    target_scene_s: float
    max_scene_s: float
    fixed_groups: list[list[int]] | None = None  # IncuBrix scene list: grouping is given
    hints: list[str | None] | None = None  # visual_hint per fixed group


class ScenePlanner(Protocol):
    name: str
    model: str

    def plan(self, req: PlanRequest) -> ScenePlan: ...
