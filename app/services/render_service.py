"""Stage 8-9: frame-exact rendering.

Every scene is rendered to its own segment per aspect ratio with identical
encoder settings and an exact frame count (from absolute narration times), then
the segments are stream-copied together and the narration is muxed once.
Consequences:
  * total frames == round(narration_s * fps) by construction -> no drift;
  * swapping one clip re-encodes one segment per ratio + a copy-concat (Advanced);
  * captions are burned per segment with a time offset, so they stay tied to the
    narration timeline rather than to clip boundaries.
"""
import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.services.caption_service import Cue
from app.utils.ffmpeg import detect_freeze, run_ffmpeg

log = logging.getLogger("app.render")


def canvas(ratio: str) -> tuple[int, int]:
    s = get_settings()
    return {
        "16:9": (s.output_width_landscape, s.output_height_landscape),
        "9:16": (s.output_width_portrait, s.output_height_portrait),
        "1:1": (s.output_width_square, s.output_height_square),
    }[ratio]


def ratio_slug(ratio: str) -> str:
    return ratio.replace(":", "x")


@dataclass
class SegmentSpec:
    scene_id: str
    clip_path: Path
    src_w: int
    src_h: int
    clip_duration_s: float
    in_point_s: float
    frames: int
    start_s: float  # scene start on the narration timeline (for caption offsets)
    saliency_cx: float
    saliency_spread: float
    luma: float
    saturation: float


def _blur_pad(W: int, H: int) -> str:
    return (f"split=2[bga][fga];[bga]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=28:2,eq=brightness=-0.06[bgb];[fga]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fgb];"
            f"[bgb][fgb]overlay=(W-w)/2:(H-h)/2")


def crop_plan(spec: SegmentSpec, W: int, H: int, force_pad: bool = False) -> tuple[str, dict]:
    """Return (filter chain from [in] to a W x H picture, description)."""
    src_ar, t_ar = spec.src_w / spec.src_h, W / H
    if abs(src_ar - t_ar) < 0.02:
        return f"scale={W}:{H}:flags=lanczos", {"policy": "scale"}
    if force_pad:
        return _blur_pad(W, H), {"policy": "blur_pad", "reason": "crop window was static; full frame shown instead"}
    if src_ar > t_ar:  # source wider: choose a horizontal window
        cw = int(spec.src_h * t_ar) // 2 * 2
        portrait_from_wide = t_ar < 1 and src_ar > 1.3
        if portrait_from_wide and spec.saliency_spread > 0.62:
            # interest is spread across the frame: a crop would cut the subject -> blurred-background pad
            return _blur_pad(W, H), {"policy": "blur_pad", "reason": f"subject spread {spec.saliency_spread:.2f} too wide to crop"}
        x = int(min(max(spec.saliency_cx * spec.src_w - cw / 2, 0), spec.src_w - cw)) // 2 * 2
        return (f"crop={cw}:{spec.src_h}:{x}:0,scale={W}:{H}:flags=lanczos",
                {"policy": "saliency_crop", "crop_x": x, "crop_w": cw, "saliency_cx": round(spec.saliency_cx, 3)})
    ch = int(spec.src_w / t_ar) // 2 * 2  # source taller: vertical window biased slightly upward
    y = int(min(max(0.45 * spec.src_h - ch / 2, 0), spec.src_h - ch)) // 2 * 2
    return f"crop={spec.src_w}:{ch}:0:{y},scale={W}:{H}:flags=lanczos", {"policy": "center_crop_vertical", "crop_y": y}


def colour_adjust(luma: float, sat: float) -> dict:
    s = get_settings()
    if not s.colour_normalise:
        return {"brightness": 0.0, "saturation": 1.0, "contrast": 1.0}
    b = max(-0.10, min(0.10, (s.target_luma - luma) * 0.6))
    sv = max(0.85, min(1.25, 0.32 / sat)) if sat > 0.02 else 1.0
    c = 1.06 if luma < 0.3 else 1.0
    return {"brightness": round(b, 3), "saturation": round(sv, 3), "contrast": c}


def _ass_time(t: float) -> str:
    cs = int(round(max(t, 0) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def write_ass(cues: list[Cue], seg_start: float, seg_dur: float, W: int, H: int, path: Path, language: str | None) -> bool:
    s = get_settings()
    font = {"hi": "Noto Sans Devanagari", "ta": "Noto Sans Tamil"}.get((language or "")[:2], s.caption_font)
    size = int(round((H * 0.048) if W > H else (W * 0.062) if H > W else (W * 0.055)))
    margin_v = int(H * (0.07 if W >= H else 0.14))
    lines = []
    for c in cues:
        a, b = max(c.start_s, seg_start), min(c.end_s, seg_start + seg_dur)
        if b - a < 0.05:
            continue
        text = c.text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")
        lines.append(f"Dialogue: 0,{_ass_time(a - seg_start)},{_ass_time(b - seg_start)},Cap,,0,0,0,,{text}")
    if not lines:
        return False
    path.write_text(
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {W}\nPlayResY: {H}\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,"
        f"{max(2, size // 14)},1,2,{int(W * 0.07)},{int(W * 0.07)},{margin_v},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return True


class SegmentQAError(RuntimeError):
    pass


def render_segment(spec: SegmentSpec, ratio: str, out: Path, cues: list[Cue] | None, language: str | None) -> dict:
    """Render, then QA the segment itself. A crop can isolate a static part of a moving
    clip; if so, re-render once showing the full frame (blur-pad) before giving up."""
    info = _render_segment(spec, ratio, out, cues, language, force_pad=False)
    if _segment_frozen(out) and info["crop"]["policy"] not in ("blur_pad", "scale"):
        info = _render_segment(spec, ratio, out, cues, language, force_pad=True)
        info["self_healed"] = "static crop window replaced by full-frame blur-pad"
    if _segment_frozen(out):
        raise SegmentQAError(f"{spec.scene_id} {ratio}: segment has frozen frames")
    return info


def _segment_frozen(path: Path) -> bool:
    return bool(detect_freeze(path, 1.0, -60, pre_vf="crop=iw:ih*0.72:0:0"))


def _render_segment(spec: SegmentSpec, ratio: str, out: Path, cues: list[Cue] | None, language: str | None,
                    force_pad: bool) -> dict:
    s = get_settings()
    fps = s.output_fps
    W, H = canvas(ratio)
    need_s = spec.frames / fps
    avail = max(0.05, spec.clip_duration_s - spec.in_point_s)
    speed, loop = 1.0, False
    if avail + 0.02 < need_s:
        if avail * 1.5 >= need_s:
            speed = round(need_s / avail + 0.01, 4)  # gentle slow-motion instead of a visible loop
        else:
            loop = True
    crop_chain, crop_info = crop_plan(spec, W, H, force_pad)
    col = colour_adjust(spec.luma, spec.saturation)
    pre = f"setpts={speed}*(PTS-STARTPTS)," if speed != 1.0 else "setpts=PTS-STARTPTS,"
    chain = (f"[0:v]{pre}fps={fps},{crop_chain},setsar=1,"
             f"eq=brightness={col['brightness']}:saturation={col['saturation']}:contrast={col['contrast']},"
             f"format=yuv420p")
    ass_name = None
    if cues is not None:
        ass_path = out.with_suffix(".ass")
        if write_ass(cues, spec.start_s, need_s, W, H, ass_path, language):
            ass_name = ass_path.name
            chain += f",ass={ass_name}"
    chain += ",tpad=stop_mode=clone:stop_duration=1[v]"
    args = []
    if loop:
        args += ["-stream_loop", "-1"]
    args += ["-ss", f"{spec.in_point_s:.3f}", "-i", str(spec.clip_path.resolve()), "-filter_complex", chain,
             "-map", "[v]", "-frames:v", str(spec.frames), "-r", str(fps), "-an",
             "-c:v", "libx264", "-preset", s.video_preset, "-crf", str(s.video_crf), "-profile:v", "high",
             "-pix_fmt", "yuv420p", "-g", str(fps * 2), "-video_track_timescale", str(fps * 512),
             "-threads", str(s.ffmpeg_threads), str(out.resolve())]
    run_ffmpeg(args, cwd=out.parent)
    return {"crop": crop_info, "colour": col, "speed": speed, "looped": loop, "captions_burned": ass_name is not None}


def concat_and_mux(segments: list[Path], narration_wav: Path, total_frames: int, out: Path) -> None:
    s = get_settings()
    lst = out.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in segments))
    video_only = out.with_suffix(".video.mp4")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(video_only)])
    total_s = total_frames / s.output_fps
    run_ffmpeg(["-i", str(video_only), "-i", str(narration_wav), "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", s.audio_bitrate, "-ar", "48000", "-af", "apad",
                "-t", f"{total_s:.6f}", "-movflags", "+faststart", str(out)])
    video_only.unlink(missing_ok=True)
    lst.unlink(missing_ok=True)
