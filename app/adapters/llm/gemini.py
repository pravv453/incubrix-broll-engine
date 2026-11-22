"""Gemini scene planner with structured output (response_schema = ScenePlan).
The model only groups sentence indices and writes visual briefs; Python
validates the grouping and owns all timing."""
import json
import logging

from app.adapters.gemini_client import gemini_client, require_model
from app.adapters.llm.base import PlanRequest
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.schemas.scenes import ScenePlan
from app.services.segmentation import CoverageError, validate_grouping

log = logging.getLogger("app.planner")

SYSTEM = """You plan B-roll footage for a narrated video. You receive numbered narration spans
(sentences or phrases) with durations. Return JSON matching the schema.

Rules:
- Group CONSECUTIVE span indices into scenes. Every index appears exactly once, in order.
  Aim for scenes of about {target:.0f} s; never exceed {max:.0f} s unless a single span is longer.
  Start a new scene when the visual idea changes.
- visual_concept: one concrete, observable shot (subject, action, setting) that matches the MEANING,
  not a keyword. "cloud computing" -> server racks, not sky. "bank" by a river -> riverbank.
- primary_query: 2-5 plain ENGLISH words for a stock-footage search, even if narration is Hindi/Tamil.
  alternative_queries: up to 3 other phrasings. must_avoid: misleading literal readings (e.g. "sky").
- Abstract lines: set is_abstract=true and choose an honest illustrative metaphor.
- Numbers/statistics: has_statistic=true; use neutral data/work visuals, never invent a specific event.
- Named real people or brands: list them in named_entities; NEVER put names, logos or brands in queries;
  use generic visuals.
- Sad or sensitive news: sensitivity "sensitive" or "tragic"; calm, respectful, non-graphic visuals.
- Respect the requested style and the avoid list.
"""


class GeminiPlanner:
    name = "gemini"

    def __init__(self) -> None:
        self.model = require_model(get_settings().gemini_text_model, "GEMINI_TEXT_MODEL")

    def plan(self, req: PlanRequest) -> ScenePlan:
        from google.genai import types

        payload = {
            "style": req.style,
            "avoid": req.avoid,
            "language": req.language,
            "spans": [{"index": s.index, "text": s.text, "duration_s": round(s.duration_s, 2)} for s in req.spans],
        }
        if req.fixed_groups:
            payload["fixed_scenes"] = [{"span_indices": g, "visual_hint": (req.hints or [None] * len(req.fixed_groups))[i]}
                                       for i, g in enumerate(req.fixed_groups)]
            payload["note"] = "Scene grouping is FIXED: return exactly these span_indices, one scene each, in order. Use the visual_hint as the strongest guide."
        prompt = json.dumps(payload, ensure_ascii=False)
        last_err = ""
        for attempt in range(2):
            try:
                resp = gemini_client().models.generate_content(
                    model=self.model,
                    contents=prompt + (f"\n\nPrevious answer was invalid: {last_err}. Fix it." if last_err else ""),
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM.format(target=req.target_scene_s, max=req.max_scene_s),
                        response_mime_type="application/json",
                        response_schema=ScenePlan,
                        temperature=0.2,
                        seed=get_settings().seed,
                    ),
                )
                plan = ScenePlan.model_validate_json(resp.text)
                groups = [s.span_indices for s in plan.scenes]
                if req.fixed_groups and groups != req.fixed_groups:
                    if len(groups) != len(req.fixed_groups):
                        raise CoverageError("fixed scene grouping was not preserved")
                    for s, g in zip(plan.scenes, req.fixed_groups):
                        s.span_indices = g
                validate_grouping([s.span_indices for s in plan.scenes], len(req.spans))
                return plan
            except (CoverageError, ValueError) as exc:
                last_err = str(exc)[:300]
                log.warning("planner_invalid_output", extra={"attempt": attempt, "error": last_err})
            except AppError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = type(exc).__name__
                log.warning("planner_call_failed", extra={"attempt": attempt, "error": last_err})
        raise AppError(ErrorCode.SCENE_PLANNING_FAILED, details={"reason": last_err})
