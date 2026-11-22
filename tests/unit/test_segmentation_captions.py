import re

import pytest

from app.schemas.transcript import Transcript, Word
from app.services.caption_service import build_cues, to_srt
from app.services.segmentation import (
    CoverageError,
    build_scene_spans,
    enforce_min_duration,
    split_sentences,
    validate_coverage,
    validate_grouping,
)
from app.utils.timecode import s_to_frame, srt_timestamp

FPS = 30


def make_transcript(text: str, wps: float = 2.5, sentence_pause: float = 0.4, lead: float = 0.3, tail: float = 0.5):
    """Synthetic word timings: constant speaking rate, pauses after sentences."""
    words, t = [], lead
    for tok in text.split():
        d = 1 / wps
        words.append(Word(text=tok, start_s=round(t, 3), end_s=round(t + d * 0.85, 3)))
        t += d + (sentence_pause if tok.endswith((".", "!", "?", "।")) else 0)
    return Transcript(duration_s=round(t + tail, 3), words=words, language="en")


SCRIPT = (
    "Trust is earned slowly. Sales grew 40% in 2025, driven by small shops adopting cloud computing. "
    "A mouse sits beside the laptop. Python developers automate the boring parts. "
    "Value is what you keep when nobody is watching."
)


def test_sentence_spans_follow_punctuation():
    t = make_transcript(SCRIPT)
    spans = split_sentences(t.words, max_span_s=20)
    texts = [" ".join(w.text for w in t.words[s.first : s.last + 1]) for s in spans]
    assert texts[0] == "Trust is earned slowly."
    assert all(x.endswith(".") for x in texts)
    assert len(spans) == 5


def test_long_sentence_split_at_phrase_boundary():
    t = make_transcript(SCRIPT)
    spans = split_sentences(t.words, max_span_s=3.0)
    parts = [" ".join(w.text for w in t.words[s.first : s.last + 1]) for s in spans]
    # a sentence with a comma splits at the comma
    assert "Sales grew 40% in 2025," in parts
    # spans never cross a sentence boundary
    for s in spans:
        inner = t.words[s.first : s.last]
        assert not any(w.text.endswith(".") for w in inner)
    assert all(t.words[s.last].end_s - t.words[s.first].start_s <= 3.0 for s in spans if s.last - s.first >= 3)


def test_unpunctuated_long_sentence_splits_at_longest_pause():
    t = make_transcript("Value is what you keep when nobody is watching and nobody is counting.")
    # insert a deliberate breath after "keep"
    k = [w.text for w in t.words].index("keep")
    shifted = [
        w if i <= k else Word(text=w.text, start_s=w.start_s + 0.5, end_s=w.end_s + 0.5)
        for i, w in enumerate(t.words)
    ]
    t2 = Transcript(duration_s=t.duration_s + 0.5, words=shifted)
    spans = split_sentences(t2.words, max_span_s=3.0)
    assert spans[0].last == k


def test_hindi_danda_is_a_sentence_boundary():
    t = make_transcript("बचत करना ज़रूरी है। हर महीने थोड़ा बचाइए।")
    assert len(split_sentences(t.words)) == 2


def test_full_coverage_and_cuts_in_pauses():
    t = make_transcript(SCRIPT)
    spans = split_sentences(t.words, max_span_s=20)
    scenes = build_scene_spans(t, spans, [[i] for i in range(len(spans))], FPS)
    validate_coverage(scenes, t, FPS)
    assert scenes[0].start_frame == 0
    assert scenes[-1].end_frame == s_to_frame(t.duration_s, FPS)
    for a, b in zip(scenes, scenes[1:]):
        cut_s = a.end_frame / FPS
        # cut lies strictly inside the silence between the two scenes' words
        assert t.words[a.last_word].end_s <= cut_s <= t.words[b.first_word].start_s + 1 / FPS


def test_grouping_must_be_ordered_partition():
    validate_grouping([[0, 1], [2]], 3)
    for bad in ([[0], [2]], [[1], [0]], [[0, 2], [1]], [[0], [], [1]]):
        with pytest.raises(CoverageError):
            validate_grouping(bad, 3)


def test_short_scenes_are_merged():
    t = make_transcript("Yes. No. Saving money is a habit you can build every single day.")
    spans = split_sentences(t.words)
    scenes = build_scene_spans(t, spans, [[i] for i in range(len(spans))], FPS)
    merged = enforce_min_duration(scenes, FPS, min_scene_s=1.5)
    validate_coverage(merged, t, FPS)
    assert all(s.frames >= s_to_frame(1.5, FPS) for s in merged)
    assert len(merged) < len(scenes)


def test_validate_coverage_detects_gap():
    t = make_transcript(SCRIPT)
    spans = split_sentences(t.words, max_span_s=20)
    scenes = build_scene_spans(t, spans, [[i] for i in range(len(spans))], FPS)
    scenes[1].start_frame += 3
    with pytest.raises(CoverageError):
        validate_coverage(scenes, t, FPS)


def test_srt_format_and_ordering():
    t = make_transcript(SCRIPT)
    cues = build_cues(t.words, max_chars=42, max_words=8)
    srt = to_srt(cues)
    blocks = srt.strip().split("\n\n")
    assert len(blocks) == len(cues)
    for b, c in zip(blocks, cues):
        lines = b.split("\n")
        assert lines[0] == str(c.index)
        assert re.fullmatch(r"\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}", lines[1])
        assert len(lines[2]) <= 42 or len(lines[2].split()) == 1
    for a, b in zip(cues, cues[1:]):
        assert a.end_s <= b.start_s
    assert cues[-1].end_s <= t.duration_s
    # every word appears exactly once, in order
    assert " ".join(c.text for c in cues) == t.text


def test_srt_timestamp():
    assert srt_timestamp(3661.5) == "01:01:01,500"
    assert srt_timestamp(0) == "00:00:00,000"
