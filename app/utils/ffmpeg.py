"""Thin, explicit wrappers around ffmpeg/ffprobe. No hidden magic: every command
is built as an argv list (never a shell string) so user data can't inject flags."""
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings

log = logging.getLogger("app.ffmpeg")


class FFmpegError(RuntimeError):
    def __init__(self, msg: str, stderr: str = ""):
        super().__init__(msg)
        self.stderr = stderr[-2000:]


def _bin(name: str) -> str:
    s = get_settings()
    return s.ffmpeg_bin if name == "ffmpeg" else s.ffprobe_bin


def run_ffmpeg(args: list[str], timeout: float = 900, cwd: str | Path | None = None) -> str:
    cmd = [_bin("ffmpeg"), "-hide_banner", "-nostdin", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({proc.returncode})", proc.stderr)
    return proc.stderr


def ffprobe_json(path: str | Path) -> dict:
    cmd = [
        _bin("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise FFmpegError("ffprobe could not read the file", proc.stderr)
    return json.loads(proc.stdout or "{}")


@dataclass
class MediaInfo:
    duration_s: float
    format_name: str
    has_audio: bool
    has_video: bool
    audio_codec: str | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pix_fmt: str | None = None
    nb_frames: int | None = None
    sample_rate: int | None = None
    audio_duration_s: float | None = None
    video_duration_s: float | None = None


def _ratio(s: str | None) -> float | None:
    if not s or s in ("0/0",):
        return None
    if "/" in s:
        n, d = s.split("/")
        return float(n) / float(d) if float(d) else None
    return float(s)


def probe(path: str | Path) -> MediaInfo:
    data = ffprobe_json(path)
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info = MediaInfo(
        duration_s=float(fmt.get("duration") or 0.0),
        format_name=fmt.get("format_name", ""),
        has_audio=a is not None,
        has_video=v is not None,
    )
    if a:
        info.audio_codec = a.get("codec_name")
        info.sample_rate = int(a.get("sample_rate") or 0) or None
        info.audio_duration_s = float(a["duration"]) if a.get("duration") else None
    if v:
        info.video_codec = v.get("codec_name")
        info.width, info.height = v.get("width"), v.get("height")
        info.fps = _ratio(v.get("avg_frame_rate")) or _ratio(v.get("r_frame_rate"))
        info.pix_fmt = v.get("pix_fmt")
        info.nb_frames = int(v["nb_frames"]) if str(v.get("nb_frames", "")).isdigit() else None
        info.video_duration_s = float(v["duration"]) if v.get("duration") else None
    return info


# ---------------------------------------------------------------- detectors
def detect_black(path: str | Path, min_s: float = 0.25, start: float | None = None, dur: float | None = None) -> list[tuple[float, float]]:
    """Intervals of (near) black picture."""
    args = _window(start, dur) + ["-i", str(path), "-vf", f"blackdetect=d={min_s}:pic_th=0.97:pix_th=0.08", "-an", "-f", "null", "-"]
    err = run_ffmpeg(args)
    return [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)]


def detect_freeze(path: str | Path, min_s: float = 1.0, noise_db: float = -60, start: float | None = None,
                  dur: float | None = None, pre_vf: str = "") -> list[tuple[float, float | None]]:
    """Intervals where the picture does not change. A freeze still open at EOF has end=None.
    `pre_vf` lets the final QA ignore the caption band (captions change even over a frozen picture)."""
    vf = (pre_vf + "," if pre_vf else "") + f"freezedetect=n={noise_db}dB:d={min_s}"
    args = _window(start, dur) + ["-i", str(path), "-vf", vf, "-an", "-f", "null", "-"]
    err = run_ffmpeg(args)
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", err)]
    return [(s, ends[i] if i < len(ends) else None) for i, s in enumerate(starts)]


def volume_stats(path: str | Path) -> tuple[float, float]:
    """(mean_volume_db, max_volume_db) of the first audio stream."""
    err = run_ffmpeg(["-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"])
    mean = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", err)
    mx = re.search(r"max_volume: (-?[\d.]+|-inf) dB", err)
    f = lambda m: float("-inf") if not m or m.group(1) == "-inf" else float(m.group(1))  # noqa: E731
    return f(mean), f(mx)


def luma_stats(path: str | Path, start: float = 0.0, dur: float = 3.0) -> tuple[float, float]:
    """Mean luma (0-1) and mean saturation (0-1, approx) over a window, via signalstats."""
    err = run_ffmpeg(
        _window(start, dur)
        + ["-i", str(path), "-vf", "fps=2,scale=160:-2,signalstats,metadata=print", "-an", "-f", "null", "-"]
    )
    y = [float(x) for x in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", err)]
    sat = [float(x) for x in re.findall(r"lavfi\.signalstats\.SATAVG=([\d.]+)", err)]
    bits = 255.0
    return (sum(y) / len(y) / bits if y else 0.5, sum(sat) / len(sat) / 128.0 if sat else 0.3)


def _window(start: float | None, dur: float | None) -> list[str]:
    out: list[str] = []
    if start:
        out += ["-ss", f"{start:.3f}"]
    if dur:
        out += ["-t", f"{dur:.3f}"]
    return out


# ---------------------------------------------------------------- frames
def extract_frame_jpg(path: str | Path, t: float, out: str | Path, width: int = 384) -> Path:
    run_ffmpeg(["-ss", f"{max(t, 0):.3f}", "-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)])
    return Path(out)


def gray_frames(path: str | Path, times: list[float], w: int, h: int) -> list[list[int]]:
    """Decode small grayscale frames (row-major ints) at given times, for hashing/saliency."""
    frames = []
    for t in times:
        cmd = [
            _bin("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(path),
            "-frames:v", "1", "-vf", f"scale={w}:{h}:flags=area,format=gray", "-f", "rawvideo", "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode == 0 and len(proc.stdout) >= w * h:
            frames.append(list(proc.stdout[: w * h]))
    return frames


def dhash(path: str | Path, t: float) -> str | None:
    """64-bit difference hash of the frame at t (perceptual duplicate detection)."""
    fr = gray_frames(path, [t], 9, 8)
    if not fr:
        return None
    px = fr[0]
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def saliency_center_x(path: str | Path, start: float, dur: float, w: int = 64, h: int = 36) -> tuple[float, float]:
    """Horizontal centre (0-1) and spread (0-1) of visual interest across the window.

    Interest = temporal difference (motion) + spatial gradient (detail), summed per
    column over a few sampled frames. Cheap, dependency-free, deterministic."""
    times = [start + dur * f for f in (0.15, 0.35, 0.55, 0.75, 0.9)]
    frames = gray_frames(path, times, w, h)
    if not frames:
        return 0.5, 1.0
    col = [0.0] * w
    for i, fr in enumerate(frames):
        prev = frames[i - 1] if i else None
        for y in range(h):
            base = y * w
            for x in range(1, w):
                g = abs(fr[base + x] - fr[base + x - 1])
                m = abs(fr[base + x] - prev[base + x]) if prev else 0
                col[x] += g + 2 * m
    total = sum(col) or 1.0
    cx = sum(i * v for i, v in enumerate(col)) / total / (w - 1)
    var = sum(((i / (w - 1)) - cx) ** 2 * v for i, v in enumerate(col)) / total
    return cx, min(1.0, (var ** 0.5) * 3.4)  # spread ~1 for uniform interest


def load_audio_16k(path: str | Path):
    """Decode to 16 kHz mono float32 numpy array with our own ffmpeg (used for Whisper and
    Silero VAD instead of PyAV, whose API changes have broken faster-whisper's decoder)."""
    import numpy as np

    cmd = [_bin("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
           "-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise FFmpegError("audio decode failed", proc.stderr.decode(errors="replace"))
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()
