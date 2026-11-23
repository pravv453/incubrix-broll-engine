"""Opt-in tests against the real Gemini API. Skipped unless LIVE_GEMINI_API_KEY is set
(conftest blanks GEMINI_API_KEY so the default suite never calls out).
    LIVE_GEMINI_API_KEY=... uv run --group ml pytest tests/live -m live"""
import os
import wave

import pytest

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("LIVE_GEMINI_API_KEY"), reason="LIVE_GEMINI_API_KEY not set")]


@pytest.fixture
def gemini_env(monkeypatch):
    from app.adapters import gemini_client as gc
    from app.core.config import get_settings

    monkeypatch.setenv("GEMINI_API_KEY", os.environ["LIVE_GEMINI_API_KEY"])
    monkeypatch.setenv("GEMINI_TTS_MODEL", os.environ.get("LIVE_GEMINI_TTS_MODEL", "gemini-3.8-flash-tts"))
    get_settings.cache_clear()
    gc._clients.clear()
    yield
    get_settings.cache_clear()
    gc._clients.clear()


def test_gemini_tts_returns_speech(gemini_env, tmp_path):
    from app.adapters.tts.gemini_tts import GeminiTTS

    out = tmp_path / "n.wav"
    info = GeminiTTS().synthesize("Saving money starts with small habits.", "en-IN-female", "en", out)
    with wave.open(str(out)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getnframes() / w.getframerate() > 1.0
    assert info["provider"] == "gemini"
