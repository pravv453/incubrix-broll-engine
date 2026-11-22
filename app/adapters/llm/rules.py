"""Deterministic rule-based planner. Default when no LLM key is configured and
the mock used by tests. Groups sentence spans toward a target scene length and
writes concrete visual briefs using the content-policy lexicons."""
from app.adapters.llm.base import PlanRequest
from app.schemas.scenes import SceneBrief, ScenePlan
from app.services import content_policy as cp


def group_spans(spans, target_s: float, max_s: float) -> list[list[int]]:
    groups: list[list[int]] = []
    cur: list[int] = []
    dur = 0.0
    for sp in spans:
        if cur and (dur >= target_s * 0.5 or dur + sp.duration_s > max_s):
            groups.append(cur)
            cur, dur = [], 0.0
        cur.append(sp.index)
        dur += sp.duration_s
    if cur:
        groups.append(cur)
    return groups


def brief_for(text: str, hint: str | None, style: str) -> SceneBrief:
    ents = cp.named_entities(text)
    sens = cp.sensitivity(text)
    stat = cp.has_statistic(text)
    amb = cp.disambiguate(text)
    abstract = cp.abstract_concept(text)
    kws = cp.keywords(cp.strip_entities(text, ents))
    reasons = []
    must_avoid: list[str] = []
    if hint:
        concept, query = hint, hint
        reasons.append("IncuBrix visual_hint used as the primary brief")
    elif amb:
        word, concept = amb
        query = concept
        reasons.append(f"'{word}' is ambiguous; context resolved it to: {concept}")
        must_avoid += {"cloud": ["sky", "weather"], "mouse": ["rodent"], "python": ["snake"],
                       "bank": [], "value": ["cash pile"]}.get(word, [])
    elif stat:
        concept = "analyst reviewing business charts and reports on a laptop"
        query = "business charts laptop"
        reasons.append("statistic detected: neutral data visual, no invented factual imagery")
    elif abstract:
        concept, query = abstract, abstract
        reasons.append("abstract line: illustrative visual metaphor")
    else:
        concept = " ".join(kws[:4]) or "city life everyday"
        query = " ".join(kws[:3]) or "city life"
    if ents:
        reasons.append(f"named entity {ents}: neutral generic visual, no likeness or logo")
        must_avoid += ["logo", "brand", "celebrity"]
        query = cp.strip_entities(query, ents) or "office work"
    if sens != "none":
        reasons.append(f"{sens} topic: respectful, calm visuals")
        must_avoid += cp.SENSITIVE_BLOCK
        if not hint:
            concept = {"tragic": "candles glowing softly in a dark quiet room",
                       "sensitive": "quiet empty street in soft morning light"}[sens] if not amb else concept
            query = concept
    alt = [k for k in kws if k not in query][:2]
    return SceneBrief(
        span_indices=[0],
        visual_concept=f"{concept} ({style})" if style and not hint else concept,
        primary_query=" ".join(query.split()[: 8 if hint else 5])[:100],
        alternative_queries=alt,
        must_avoid=list(dict.fromkeys(must_avoid)),
        sensitivity=sens,
        named_entities=ents,
        is_abstract=bool(abstract) and not hint and not amb and not stat,
        has_statistic=stat,
        planning_reason="; ".join(reasons) or "keywords from narration",
    )


class RulesPlanner:
    name, model = "rules", "rules-v1"

    def plan(self, req: PlanRequest) -> ScenePlan:
        groups = req.fixed_groups or group_spans(req.spans, req.target_scene_s, req.max_scene_s)
        scenes = []
        for gi, g in enumerate(groups):
            text = " ".join(req.spans[i].text for i in g)
            hint = req.hints[gi] if req.hints else None
            b = brief_for(text, hint, req.style)
            b.span_indices = g
            scenes.append(b)
        return ScenePlan(scenes=scenes, language=req.language)
