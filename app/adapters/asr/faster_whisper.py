"""faster-whisper (CTranslate2 Whisper, MIT) with word timestamps + Silero VAD filter."""
import logging
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.schemas.transcript import Transcript, Word

log = logging.getLogger("app.asr")
_model_cache: dict[str, object] = {}


class FasterWhisperASR:
    name = "faster_whisper"

    def __init__(self) -> None:
        s = get_settings()
        self.model = s.asr_model
        self.compute_type = s.asr_compute_type
        self.cache_dir = s.model_cache_dir

    def _load(self):
        key = f"{self.model}:{self.compute_type}"
        if key not in _model_cache:
            from faster_whisper import WhisperModel

            _model_cache[key] = WhisperModel(self.model, device="cpu", compute_type=self.compute_type,
                                             download_root=self.cache_dir)
        return _model_cache[key]

    def transcribe(self, audio_path: Path, duration_s: float, language=None, hint_text=None) -> Transcript:
        try:
            from app.utils.ffmpeg import load_audio_16k

            model = self._load()
            segments, info = model.transcribe(
                load_audio_16k(audio_path), language=language, word_timestamps=True, vad_filter=True,
                initial_prompt=(hint_text[:400] if hint_text else None), beam_size=5,
                condition_on_previous_text=False,
            )
            words: list[Word] = []
            for seg in segments:
                if seg.no_speech_prob > 0.8 and seg.avg_logprob < -1.0:
                    continue  # likely hallucination over non-speech
                for w in seg.words or []:
                    text = w.word.strip()
                    if not text:
                        continue
                    start = max(0.0, min(w.start, duration_s))
                    end = max(start, min(w.end, duration_s))
                    if words and start < words[-1].start_s:
                        start = words[-1].start_s
                        end = max(end, start)
                    words.append(Word(text=text, start_s=round(start, 3), end_s=round(end, 3),
                                      confidence=round(w.probability, 3)))
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("asr_failed")
            raise AppError(ErrorCode.TRANSCRIPTION_FAILED, details={"reason": type(exc).__name__}) from exc
        return Transcript(language=info.language, duration_s=duration_s, words=words,
                          provider=self.name, model=f"whisper-{self.model} ({self.compute_type})")
