"""Gemini TTS via the official google-genai SDK. Returns 24 kHz 16-bit mono PCM,
which we wrap into WAV. Long scripts are chunked at sentence boundaries."""
import io
import logging
import re
import time
import wave
from pathlib import Path

from app.adapters.gemini_client import gemini_client, require_model
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

log = logging.getLogger("app.tts")
MAX_CHUNK = 1500


def _chunks(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?।])\s+", text.strip())
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > MAX_CHUNK:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


class GeminiTTS:
    name = "gemini"

    def __init__(self) -> None:
        s = get_settings()
        self.model = require_model(s.gemini_tts_model, "GEMINI_TTS_MODEL")
        self.voice_map = s.voice_map

    def synthesize(self, text: str, voice: str, language: str | None, out_path: Path) -> dict:
        from google.genai import types

        voice_name = self.voice_map.get(voice, voice if voice[:1].isupper() else "Kore")
        client = gemini_client()
        pcm = bytearray()
        rate = 24000
        for chunk in _chunks(text):
            for attempt in range(3):
                try:
                    resp = client.models.generate_content(
                        model=self.model,
                        contents=chunk,
                        config=types.GenerateContentConfig(
                            response_modalities=["AUDIO"],
                            speech_config=types.SpeechConfig(
                                voice_config=types.VoiceConfig(
                                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                                )
                            ),
                        ),
                    )
                    part = resp.candidates[0].content.parts[0].inline_data
                    m = re.search(r"rate=(\d+)", part.mime_type or "")
                    rate = int(m.group(1)) if m else rate
                    pcm.extend(part.data)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("tts_chunk_failed", extra={"attempt": attempt, "error": type(exc).__name__})
                    if attempt == 2:
                        raise AppError(ErrorCode.TTS_FAILED, details={"reason": type(exc).__name__}) from exc
                    time.sleep(2 ** attempt)
            pcm.extend(b"\x00\x00" * int(rate * 0.25))  # breath between chunks
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(pcm))
        out_path.write_bytes(buf.getvalue())
        return {"provider": self.name, "model": self.model, "voice": voice_name, "characters": len(text)}
