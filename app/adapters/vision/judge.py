"""Independent relevance judge (Outstanding level). Deliberately a *different*
model family from the selector (CLIP), so its score is not self-grading."""
import hashlib
import json
import logging
from pathlib import Path

from app.core.config import get_settings

log = logging.getLogger("app.judge")

PROMPT = """You are rating B-roll for a narrated video. Narration for this scene:
"{narration}"
The frames are sampled from the clip shown during that narration.
Rate how well the footage fits the MEANING of the narration (not keyword overlap), 1-5:
5 = clearly illustrates it; 4 = good fit; 3 = acceptable/generic; 2 = weak; 1 = wrong or misleading.
Also flag safety problems (graphic content, visible brand logos, recognisable real people).
Return JSON: {{"score": int, "rationale": str, "safety_flags": [str]}}"""


class MockJudge:
    name, model = "mock", "mock-judge-1"

    def judge(self, narration: str, frames: list[Path]) -> dict:
        h = int(hashlib.md5(narration.encode()).hexdigest()[:4], 16)
        return {"score": 3 + h % 3, "rationale": "mock judge", "safety_flags": []}


class GeminiJudge:
    name = "gemini"

    def __init__(self) -> None:
        from app.adapters.gemini_client import require_model

        self.model = require_model(get_settings().gemini_judge_model, "GEMINI_JUDGE_MODEL")

    def judge(self, narration: str, frames: list[Path]) -> dict:
        from google.genai import types

        from app.adapters.gemini_client import gemini_client

        parts = [types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg") for p in frames]
        parts.append(PROMPT.format(narration=narration))
        resp = gemini_client().models.generate_content(
            model=self.model, contents=parts,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        data = json.loads(resp.text)
        data["score"] = int(max(1, min(5, int(data.get("score", 0)))))
        return data


def get_judge():
    p = get_settings().judge_provider
    if p == "gemini":
        return GeminiJudge()
    if p == "mock":
        return MockJudge()
    return None
