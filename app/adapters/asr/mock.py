"""Deterministic ASR for tests: spreads the known script (or filler words) over
the audio with a constant speaking rate and pauses after sentences."""
from pathlib import Path

from app.schemas.transcript import Transcript, Word

LEAD_S, WORD_S, PAUSE_S = 0.3, 0.42, 0.35
SENT_END = (".", "!", "?", "।")


def nominal_duration(text: str) -> float:
    toks = text.split()
    return LEAD_S + len(toks) * WORD_S + sum(t.endswith(SENT_END) for t in toks) * PAUSE_S + 0.5


class MockASR:
    name, model = "mock", "mock-asr-1"

    def transcribe(self, audio_path: Path, duration_s: float, language=None, hint_text=None) -> Transcript:
        text = hint_text
        if not text:
            n = max(4, int((duration_s - 0.8) / WORD_S))
            filler = "Mock narration about everyday work and city life continues here."
            base = filler.split()
            text = " ".join(base[i % len(base)] for i in range(n))
            if not text.endswith("."):
                text += "."
        toks = text.split()
        scale = min(1.0, (duration_s - 0.2) / nominal_duration(text)) if nominal_duration(text) > 0 else 1.0
        t, words = LEAD_S * scale, []
        for tok in toks:
            d = WORD_S * scale
            words.append(Word(text=tok, start_s=round(t, 3), end_s=round(t + d * 0.86, 3), confidence=0.99))
            t += d + (PAUSE_S * scale if tok.endswith(SENT_END) else 0)
        return Transcript(language=language or "en", duration_s=duration_s, words=words,
                          provider=self.name, model=self.model)
