"""Mock TTS: a speech-like amplitude-modulated tone whose length matches the
mock ASR's speaking-rate model, so script->audio->transcript is consistent."""
from pathlib import Path

from app.adapters.asr.mock import nominal_duration
from app.utils.ffmpeg import run_ffmpeg


class MockTTS:
    name, model = "mock", "mock-tts-1"

    def synthesize(self, text: str, voice: str, language: str | None, out_path: Path) -> dict:
        dur = nominal_duration(text)
        expr = "0.25*sin(2*PI*180*t)*(0.55+0.45*sin(2*PI*3.1*t))*(0.6+0.4*sin(2*PI*0.7*t))"
        run_ffmpeg(["-f", "lavfi", "-i", f"aevalsrc={expr}:s=48000:d={dur:.3f}", "-ac", "1", str(out_path)])
        return {"provider": self.name, "model": self.model, "voice": voice, "characters": len(text), "synthetic": True}
