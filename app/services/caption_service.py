"""SRT captions from word timestamps. Captions follow the narration, never the clips."""
from dataclasses import dataclass

from app.schemas.transcript import Word
from app.services.segmentation import SENTENCE_END
from app.utils.timecode import srt_timestamp

PAUSE_BREAK_S = 0.6
MIN_CUE_S = 0.8


@dataclass
class Cue:
    index: int
    start_s: float
    end_s: float
    text: str


def build_cues(words: list[Word], max_chars: int = 42, max_words: int = 8) -> list[Cue]:
    groups: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            text_len = len(" ".join(x.text for x in cur + [w]))
            pause = w.start_s - cur[-1].end_s
            if text_len > max_chars or len(cur) >= max_words or pause > PAUSE_BREAK_S:
                groups.append(cur)
                cur = []
        cur.append(w)
        if w.text.strip().endswith(SENTENCE_END):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    cues: list[Cue] = []
    for i, g in enumerate(groups):
        start = g[0].start_s
        end = max(g[-1].end_s, start + MIN_CUE_S)
        if i + 1 < len(groups):
            end = min(end, groups[i + 1][0].start_s)  # never overlap the next cue
        end = max(end, start + 0.05)
        cues.append(Cue(i + 1, start, end, " ".join(w.text for w in g)))
    return cues


def to_srt(cues: list[Cue]) -> str:
    return "".join(
        f"{c.index}\n{srt_timestamp(c.start_s)} --> {srt_timestamp(c.end_s)}\n{c.text}\n\n" for c in cues
    )
