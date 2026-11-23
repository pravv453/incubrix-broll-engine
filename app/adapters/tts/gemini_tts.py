"""Gemini TTS via the official google-genai SDK (Interactions API, v1beta).
Each chunk returns a 24 kHz 16-bit mono WAV; we concatenate the PCM into one WAV.
Long scripts are chunked at sentence boundaries."""
import base64
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


def _decode(audio_b64: str | bytes) -> tuple[bytes, int]:
    """Return (pcm16 mono, sample_rate) from a WAV or headerless L16 payload."""
    data = base64.b64decode(audio_b64) if isinstance(audio_b64, str) else audio_b64
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise ValueError("unexpected TTS audio format")
            return w.readframes(w.getnframes()), w.getframerate()
    return data, 24000


class GeminiTTS:
    name = "gemini"

    def __init__(self) -> None:
        s = get_settings()
        self.model = require_model(s.gemini_tts_model, "GEMINI_TTS_MODEL")
        self.voice_map = s.voice_map

    def _call(self, client, chunk: str, voice_name: str):
        return client.interactions.create(
            model=self.model,
            input=[{"type": "user_input", "content": [{"type": "text", "text": chunk}]}],
            response_format={"type": "audio"},
            generation_config={"speech_config": [{"voice": voice_name}]},
        )

    def synthesize(self, text: str, voice: str, language: str | None, out_path: Path) -> dict:
        voice_name = self.voice_map.get(voice, voice if voice[:1].isupper() else "Kore")
        client = gemini_client(api_version="v1beta")
        pcm = bytearray()
        rate = 24000
        for chunk in _chunks(text):
            for attempt in range(4):
                try:
                    it = self._call(client, chunk, voice_name)
                    frames, rate = _decode(it.output_audio.data)
                    pcm.extend(frames)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("tts_chunk_failed", extra={"attempt": attempt, "error": type(exc).__name__})
                    if attempt == 3:
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
