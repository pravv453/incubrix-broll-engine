"""Shared google-genai client (official SDK). Created lazily so no key is needed in tests."""
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

_client = None


def gemini_client():
    global _client
    s = get_settings()
    if not s.gemini_api_key:
        raise AppError(ErrorCode.INTERNAL_ERROR, message="GEMINI_API_KEY is not set but a Gemini provider is selected")
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=s.gemini_api_key)
    return _client


def require_model(value: str, setting: str) -> str:
    if not value:
        raise AppError(ErrorCode.INTERNAL_ERROR, message=f"{setting} must be set when using a Gemini provider")
    return value
