"""Deterministic scene segmentation and coverage (must-have #2).

Pipeline:
  1. `split_sentences` - sentence spans from word punctuation (handles Latin and
     Devanagari danda); over-long sentences are split at phrase boundaries.
  2. A planner (LLM or rules) may *group* consecutive sentence spans into scenes.
     It can only refer to span indices; `validate_grouping` rejects anything that
     is not an ordered, gap-free partition.
  3. `build_scene_spans` turns groups into frame-exact scenes. Each cut is placed
     at the midpoint of the pause between the last word of one scene and the
     first word of the next, so cuts land in silence. Scene 1 starts at frame 0
     and the last scene ends at the narration's final frame -> every frame of
     narration is covered by construction.
  4. `enforce_min_duration` merges scenes shorter than MIN_SCENE_S into a
     neighbour (no flashing clips), then `validate_coverage` re-checks invariants.
"""
from dataclasses import dataclass

from app.schemas.transcript import Transcript, Word
from app.utils.timecode import s_to_frame

SENTENCE_END = (".", "!", "?", "…", "।", "॥", "。")
PHRASE_END = (",", ";", ":", "—", "–")


@dataclass(frozen=True)
class WordSpan:
    """Inclusive word index range [first, last]."""

    first: int
    last: int


@dataclass
class SceneSpan:
    scene_index: int
    first_word: int
    last_word: int
    start_frame: int
    end_frame: int  # exclusive

    @property
    def frames(self) -> int:
        return self.end_frame - self.start_frame


class CoverageError(ValueError):
    pass


def _ends_with(word: Word, marks: tuple[str, ...]) -> bool:
    t = word.text.strip().rstrip("\"'”’)]")
    return t.endswith(marks)


def split_sentences(words: list[Word], max_span_s: float = 8.0) -> list[WordSpan]:
    """Sentence spans; sentences longer than max_span_s are split at phrase marks
    (or, failing that, at the longest internal pause)."""
    if not words:
        return []
    spans: list[WordSpan] = []
    start = 0
    for i, w in enumerate(words):
        if _ends_with(w, SENTENCE_END) or i == len(words) - 1:
            spans.append(WordSpan(start, i))
            start = i + 1
    out: list[WordSpan] = []
    for sp in spans:
        out.extend(_split_long(words, sp, max_span_s))
    return out


def _split_long(words: list[Word], sp: WordSpan, max_span_s: float) -> list[WordSpan]:
    dur = words[sp.last].end_s - words[sp.first].start_s
    if dur <= max_span_s or sp.last - sp.first < 3:
        return [sp]
    # candidate split points: after a phrase mark, else the longest pause
    mid_t = (words[sp.first].start_s + words[sp.last].end_s) / 2
    candidates = [i for i in range(sp.first + 1, sp.last - 1) if _ends_with(words[i], PHRASE_END)]
    if candidates:
        cut = min(candidates, key=lambda i: abs(words[i].end_s - mid_t))
    else:
        # no punctuation: longest pause wins; ties go to the point nearest the middle
        cut = max(
            range(sp.first + 1, sp.last - 1),
            key=lambda i: (round(words[i + 1].start_s - words[i].end_s, 2), -abs(words[i].end_s - mid_t)),
        )
    return _split_long(words, WordSpan(sp.first, cut), max_span_s) + _split_long(
        words, WordSpan(cut + 1, sp.last), max_span_s
    )


def validate_grouping(groups: list[list[int]], n_spans: int) -> None:
    """Groups must be an ordered partition of range(n_spans) into consecutive runs."""
    flat = [i for g in groups for i in g]
    if flat != list(range(n_spans)) or any(not g for g in groups):
        raise CoverageError(f"grouping is not an ordered, gap-free partition of {n_spans} spans: {groups}")
    for g in groups:
        if g != list(range(g[0], g[-1] + 1)):
            raise CoverageError(f"group {g} is not contiguous")


def build_scene_spans(
    transcript: Transcript, spans: list[WordSpan], groups: list[list[int]], fps: int
) -> list[SceneSpan]:
    validate_grouping(groups, len(spans))
    words = transcript.words
    total_frames = s_to_frame(transcript.duration_s, fps)
    word_ranges = [(spans[g[0]].first, spans[g[-1]].last) for g in groups]
    scenes: list[SceneSpan] = []
    prev_end = 0
    for idx, (first, last) in enumerate(word_ranges):
        if idx == len(word_ranges) - 1:
            end = total_frames
        else:
            next_first = word_ranges[idx + 1][0]
            cut_s = (words[last].end_s + words[next_first].start_s) / 2
            end = s_to_frame(cut_s, fps)
        end = max(end, prev_end + 1)
        scenes.append(SceneSpan(idx, first, last, prev_end, end))
        prev_end = end
    scenes[-1].end_frame = max(total_frames, scenes[-1].start_frame + 1)
    return scenes


def enforce_min_duration(scenes: list[SceneSpan], fps: int, min_scene_s: float) -> list[SceneSpan]:
    """Merge too-short scenes into the shorter neighbour until all pass (or one remains)."""
    min_frames = s_to_frame(min_scene_s, fps)
    scenes = [SceneSpan(**vars(s)) for s in scenes]
    while len(scenes) > 1:
        short = [i for i, s in enumerate(scenes) if s.frames < min_frames]
        if not short:
            break
        i = min(short, key=lambda k: scenes[k].frames)
        if i == 0:
            j = 1
        elif i == len(scenes) - 1:
            j = i - 1
        else:
            j = i - 1 if scenes[i - 1].frames <= scenes[i + 1].frames else i + 1
        a, b = sorted((i, j))
        merged = SceneSpan(a, scenes[a].first_word, scenes[b].last_word, scenes[a].start_frame, scenes[b].end_frame)
        scenes[a : b + 1] = [merged]
    for k, s in enumerate(scenes):
        s.scene_index = k
    return scenes


def validate_coverage(scenes: list[SceneSpan], transcript: Transcript, fps: int) -> None:
    """Hard invariants; raising here fails the job rather than shipping a gap."""
    if not scenes:
        raise CoverageError("no scenes")
    total = s_to_frame(transcript.duration_s, fps)
    if scenes[0].start_frame != 0:
        raise CoverageError("first scene does not start at frame 0")
    if scenes[-1].end_frame != total:
        raise CoverageError(f"last scene ends at {scenes[-1].end_frame}, narration has {total} frames")
    expected_word = 0
    for prev, cur in zip(scenes, scenes[1:]):
        if cur.start_frame != prev.end_frame:
            raise CoverageError(f"gap/overlap between scene {prev.scene_index} and {cur.scene_index}")
    for s in scenes:
        if s.frames <= 0:
            raise CoverageError(f"scene {s.scene_index} has no frames")
        if s.first_word != expected_word:
            raise CoverageError(f"scene {s.scene_index} skips words")
        expected_word = s.last_word + 1
    if expected_word != len(transcript.words):
        raise CoverageError("not all words are assigned to a scene")
