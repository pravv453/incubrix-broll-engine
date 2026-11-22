from app.core.config import get_settings


def get_asr():
    p = get_settings().asr_provider
    if p == "faster_whisper":
        from app.adapters.asr.faster_whisper import FasterWhisperASR
        return FasterWhisperASR()
    from app.adapters.asr.mock import MockASR
    return MockASR()
