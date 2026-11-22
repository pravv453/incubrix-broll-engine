"""Stage 0-2: validate input audio, generate narration for text, detect no-speech,
transcribe, and align script text back onto ASR timings."""
import difflib
import logging
import re
from pathlib import Path

from app.adapters.asr import get_asr
from app.adapters.tts import get_tts
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.schemas.transcript import Transcript, Word
from app.utils.ffmpeg import FFmpegError, probe, run_ffmpeg, volume_stats

log = logging.getLogger("app.input")

ALLOWED_FORMATS = {"mp3": "mp3", "wav": "wav", "mov,mp4,m4a,3gp,3g2,mj2": "m4a"}
ALLOWED_EXT = {".mp3", ".wav", ".m4a"}
ALLOWED_AUDIO_CODECS = {"mp3", "aac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_u8", "pcm_s16be"}


def validate_audio_file(path: Path, filename: str | None = None) -> dict:
    """Check the *actual* stream, not just the extension. Fast (<1 s) so the API can do it inline."""
    s = get_settings()
    ext = Path(filename or path.name).suffix.lower()
    if filename and ext and ext not in ALLOWED_EXT:
        raise AppError(ErrorCode.UNSUPPORTED_AUDIO_FORMAT, details={"received_extension": ext})
    try:
        info = probe(path)
    except FFmpegError:
        raise AppError(ErrorCode.UNSUPPORTED_AUDIO_FORMAT, details={"reason": "file is not decodable audio"}) from None
    fmt = ALLOWED_FORMATS.get(info.format_name)
    if fmt is None or not info.has_audio or info.audio_codec not in ALLOWED_AUDIO_CODECS:
        raise AppError(ErrorCode.UNSUPPORTED_AUDIO_FORMAT,
                       details={"detected_format": info.format_name, "audio_codec": info.audio_codec})
    if info.has_video and fmt != "m4a":
        raise AppError(ErrorCode.UNSUPPORTED_AUDIO_FORMAT, details={"reason": "video files are not accepted"})
    if info.duration_s <= 0.3:
        raise AppError(ErrorCode.NO_SPEECH_DETECTED, details={"reason": "audio is empty", "duration_s": info.duration_s})
    if info.duration_s > s.max_audio_duration_s + 0.5:
        raise AppError(ErrorCode.AUDIO_TOO_LONG, details={"duration_s": round(info.duration_s, 2),
                                                          "max_duration_s": s.max_audio_duration_s})
    return {"format": fmt, "duration_s": info.duration_s, "codec": info.audio_codec, "sample_rate": info.sample_rate}


def to_canonical_wav(src: Path, dst: Path) -> float:
    """48 kHz mono PCM: one canonical narration for ASR, VAD and the final mux."""
    run_ffmpeg(["-i", str(src), "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(dst)])
    return probe(dst).duration_s


def generate_narration(text: str, voice: str, language: str | None, out_wav: Path) -> tuple[float, dict]:
    tts = get_tts()
    raw = out_wav.with_name("tts_raw.wav")
    meta = tts.synthesize(text, voice, language, raw)
    dur = to_canonical_wav(raw, out_wav)
    if dur <= 0.3:
        raise AppError(ErrorCode.TTS_FAILED, details={"reason": "TTS returned empty audio"})
    meta["duration_s"] = dur
    return dur, meta


def speech_check(wav: Path, duration_s: float) -> dict:
    """Silent -> NO_SPEECH_DETECTED. Music-only -> NO_SPEECH_DETECTED via Silero VAD
    speech ratio (bundled with faster-whisper, ONNX, MIT) when the real ASR is configured."""
    s = get_settings()
    mean_db, max_db = volume_stats(wav)
    report: dict = {"mean_volume_db": mean_db, "max_volume_db": max_db, "method": "volumedetect"}
    if max_db == float("-inf") or max_db < -50:
        raise AppError(ErrorCode.NO_SPEECH_DETECTED, details={"reason": "audio is silent", **report})
    if s.asr_provider == "faster_whisper":
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        from app.utils.ffmpeg import load_audio_16k

        audio = load_audio_16k(wav)
        segs = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300))
        speech = sum(x["end"] - x["start"] for x in segs) / 16000
        ratio = speech / max(duration_s, 0.01)
        report.update(method="volumedetect+silero_vad", speech_s=round(speech, 2), speech_ratio=round(ratio, 3))
        if ratio < s.min_speech_ratio:
            raise AppError(ErrorCode.NO_SPEECH_DETECTED,
                           details={"reason": "no speech found (silence or music only)", **report})
    return report


def transcribe(wav: Path, duration_s: float, language: str | None, hint_text: str | None) -> Transcript:
    t = get_asr().transcribe(wav, duration_s, language=language, hint_text=hint_text)
    if len(t.words) < 2:
        raise AppError(ErrorCode.NO_SPEECH_DETECTED, details={"reason": "transcript is empty", "words": len(t.words)})
    confs = [w.confidence for w in t.words if w.confidence is not None]
    if confs and sum(confs) / len(confs) < 0.3 and not hint_text:
        raise AppError(ErrorCode.NO_SPEECH_DETECTED,
                       details={"reason": "no intelligible speech (possibly music or noise)",
                                "mean_word_confidence": round(sum(confs) / len(confs), 3)})
    return t


_norm_re = re.compile(r"[^\w%]+", re.UNICODE)


def _norm(tok: str) -> str:
    return _norm_re.sub("", tok.lower())


def align_script(script_tokens: list[str], asr: Transcript) -> tuple[Transcript, float]:
    """Put the *script's* words (exact spelling, numbers, punctuation) onto the ASR
    timings. Matched tokens take the ASR word's times; unmatched runs are spread
    evenly across the time between their matched neighbours."""
    a = [_norm(t) for t in script_tokens]
    b = [_norm(w.text) for w in asr.words]
    times: list[tuple[float, float] | None] = [None] * len(script_tokens)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            w = asr.words[blk.b + k]
            times[blk.a + k] = (w.start_s, w.end_s)
    for op, i1, i2, j1, j2 in sm.get_opcodes():  # replaced runs: map proportionally onto the ASR run
        if op == "replace" and j2 > j1:
            t0, t1 = asr.words[j1].start_s, asr.words[j2 - 1].end_s
            n = i2 - i1
            for k in range(n):
                times[i1 + k] = (t0 + (t1 - t0) * k / n, t0 + (t1 - t0) * (k + 1) / n)
    # fill remaining gaps by interpolation between known neighbours
    first_known = next((t for t in times if t), (0.0, 0.0))
    i = 0
    while i < len(times):
        if times[i] is None:
            j = i
            while j < len(times) and times[j] is None:
                j += 1
            lo = times[i - 1][1] if i > 0 else max(0.0, first_known[0] - 0.3)
            hi = times[j][0] if j < len(times) else min(asr.duration_s, lo + 0.35 * (j - i))
            n = j - i
            for k in range(n):
                times[i + k] = (lo + (hi - lo) * k / n, lo + (hi - lo) * (k + 1) / n)
            i = j
        else:
            i += 1
    words, prev = [], 0.0
    for tok, (s0, e0) in zip(script_tokens, times):
        s0 = max(prev, min(s0, asr.duration_s))
        e0 = max(s0, min(e0, asr.duration_s))
        words.append(Word(text=tok, start_s=round(s0, 3), end_s=round(e0, 3)))
        prev = s0
    matched = sum(blk.size for blk in sm.get_matching_blocks())
    out = asr.model_copy(update={"words": words, "timing_source": "asr_word_timestamps+script_alignment"})
    return out, round(matched / max(1, len(a)), 3)
