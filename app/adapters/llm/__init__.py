from app.core.config import get_settings


def get_planner():
    if get_settings().llm_provider == "gemini":
        from app.adapters.llm.gemini import GeminiPlanner
        return GeminiPlanner()
    from app.adapters.llm.rules import RulesPlanner
    return RulesPlanner()
