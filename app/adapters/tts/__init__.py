from app.core.config import get_settings


def get_tts():
    if get_settings().tts_provider == "gemini":
        from app.adapters.tts.gemini_tts import GeminiTTS
        return GeminiTTS()
    from app.adapters.tts.mock import MockTTS
    return MockTTS()
