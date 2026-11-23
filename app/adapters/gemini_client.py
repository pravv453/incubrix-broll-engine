"""Shared google-genai client (official SDK). Created lazily so no key is needed in tests."""
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

_clients: dict = {}


def gemini_client(api_version: str | None = None):
    s = get_settings()
    if not s.gemini_api_key:
        raise AppError(ErrorCode.INTERNAL_ERROR, message="GEMINI_API_KEY is not set but a Gemini provider is selected")
    if api_version not in _clients:
        from google import genai

        opts = {"api_version": api_version} if api_version else None
        _clients[api_version] = genai.Client(api_key=s.gemini_api_key, http_options=opts)
    return _clients[api_version]


def require_model(value: str, setting: str) -> str:
    if not value:
        raise AppError(ErrorCode.INTERNAL_ERROR, message=f"{setting} must be set when using a Gemini provider")
    return value
