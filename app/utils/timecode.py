"""Frame arithmetic. Every boundary is computed from absolute time, never by
accumulating durations, so per-scene rounding can never drift the total."""


def s_to_frame(seconds: float, fps: int) -> int:
    return int(round(seconds * fps))


def frame_to_s(frame: int, fps: int) -> float:
    return frame / fps


def srt_timestamp(seconds: float) -> str:
    ms_total = int(round(max(seconds, 0.0) * 1000))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
