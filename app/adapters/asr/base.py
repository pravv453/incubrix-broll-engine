from pathlib import Path
from typing import Protocol

from app.schemas.transcript import Transcript


class ASRProvider(Protocol):
    name: str
    model: str

    def transcribe(self, audio_path: Path, duration_s: float, language: str | None = None,
                   hint_text: str | None = None) -> Transcript:
        """Word-level transcript. `hint_text` is the known script when we generated
        the narration ourselves; real ASR may use it as a prompt, mock uses it as truth."""
        ...
