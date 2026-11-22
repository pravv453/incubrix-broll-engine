"""Scene planner contract. The planner (LLM or rules) only returns *groupings of
sentence-span indices* plus visual briefs; it never returns timestamps."""
from typing import Literal

from pydantic import BaseModel, Field

Sensitivity = Literal["none", "sensitive", "tragic"]


class SceneBrief(BaseModel):
    span_indices: list[int] = Field(min_length=1)
    visual_concept: str = Field(min_length=3, max_length=300)
    primary_query: str = Field(min_length=2, max_length=100)
    alternative_queries: list[str] = Field(default_factory=list, max_length=4)
    must_avoid: list[str] = Field(default_factory=list, max_length=40)
    sensitivity: Sensitivity = "none"
    named_entities: list[str] = Field(default_factory=list)
    is_abstract: bool = False
    has_statistic: bool = False
    planning_reason: str = ""


class ScenePlan(BaseModel):
    scenes: list[SceneBrief] = Field(min_length=1)
    language: str | None = None
