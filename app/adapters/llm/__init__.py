import logging

from app.core.config import get_settings
from app.core.errors import AppError

log = logging.getLogger("app.planner")


def get_planner():
    if get_settings().llm_provider == "gemini":
        from app.adapters.llm.gemini import GeminiPlanner
        return GeminiPlanner()
    from app.adapters.llm.rules import RulesPlanner
    return RulesPlanner()


def plan_with_fallback(planner, req):
    """Run the configured planner; if a hosted planner fails (outage, quota, invalid output),
    fall back to the deterministic rules planner so a planner outage never fails a job.
    Returns (plan, planner_used, failure_reason_or_None)."""
    try:
        return planner.plan(req), planner, None
    except AppError as exc:
        if planner.name == "rules":
            raise
        from app.adapters.llm.rules import RulesPlanner

        reason = (exc.details or {}).get("reason") or exc.code
        log.warning("planner_fallback_to_rules", extra={"planner": planner.name, "error": str(reason)})
        rules = RulesPlanner()
        return rules.plan(req), rules, str(reason)
