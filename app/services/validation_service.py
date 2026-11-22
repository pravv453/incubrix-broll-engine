"""Stage 10: post-render QA (must-have #4). A job never reports success if any check fails."""
from pathlib import Path

from app.core.config import get_settings
from app.utils.ffmpeg import detect_black, detect_freeze, probe


def validate_output(path: Path, W: int, H: int, expected_frames: int) -> dict:
    s = get_settings()
    fps = s.output_fps
    info = probe(path)
    vdur = info.video_duration_s or info.duration_s
    adur = info.audio_duration_s or 0.0
    frames = info.nb_frames if info.nb_frames is not None else round(vdur * fps)
    black = detect_black(path, 0.25)
    # ignore the bottom caption band: burned captions change even over a frozen picture
    frozen = detect_freeze(path, 1.0, -60, pre_vf="crop=iw:ih*0.72:0:0")
    drift = abs(vdur - adur)
    checks = {
        "video_codec_h264": info.video_codec == "h264",
        "audio_codec_aac": info.audio_codec == "aac",
        "pixel_format_yuv420p": info.pix_fmt == "yuv420p",
        "dimensions": (info.width, info.height) == (W, H),
        "fps_30": info.fps is not None and abs(info.fps - fps) < 0.01,
        "frame_count_exact": abs(frames - expected_frames) <= 1,
        "audio_present": info.has_audio,
        "av_drift_within_1_frame": drift <= 1 / fps + 0.024,  # + one AAC frame (1024/48k)
        "no_black_frames": not black,
        "no_frozen_frames": not frozen,
    }
    return {
        "file": path.name, "passed": all(checks.values()), "checks": checks,
        "measured": {"width": info.width, "height": info.height, "fps": info.fps, "frames": frames,
                     "expected_frames": expected_frames, "video_duration_s": round(vdur, 4),
                     "audio_duration_s": round(adur, 4), "av_drift_s": round(drift, 4),
                     "black_intervals": black, "frozen_intervals": frozen, "pix_fmt": info.pix_fmt},
    }
