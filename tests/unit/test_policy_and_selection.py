"""Content policy, rights gate, ranking, URL safety, signatures, idempotency, alignment, input validation."""
import socket
import threading

import httpx
import pytest

from app.adapters.llm.rules import brief_for
from app.core.errors import AppError, ErrorCode
from app.core.security import RateLimiter, sign_body, verify_signature
from app.db import repositories as repo
from app.schemas.footage import Attribution, FootageCandidate, Licence
from app.schemas.transcript import Transcript, Word
from app.services import content_policy as cp
from app.services import rights_service
from app.services.input_service import align_script, validate_audio_file
from app.utils import urls
from tests.fixtures import media


# ---------------------------------------------------------------- hard cases: meaning, not keywords
@pytest.mark.parametrize("text,expect,not_expect", [
    ("We moved our accounting to cloud computing last year.", "server", "sky"),
    ("Storm clouds gathered and the cloud burst over the hills.", "storm", "server"),
    ("We sat on the river bank and watched the water.", "river", "branch"),
    ("Open a savings account at your bank today.", "bank branch", "river"),
    ("Click the mouse to select the file on your laptop.", "computer mouse", "field mouse"),
    ("Learn Python to automate your spreadsheets.", "programmer", "snake"),
    ("The python snake coiled around the branch at the zoo.", "snake", "programmer"),
    ("Honesty is a value we teach our children.", "family", "chart"),
    ("The market value of the shares doubled.", "chart", "family"),
])
def test_double_meaning_words_resolve_by_context(text, expect, not_expect):
    b = brief_for(text, None, "")
    assert expect in b.visual_concept.lower()
    assert not_expect not in b.visual_concept.lower()


def test_abstract_line_gets_illustrative_metaphor():
    b = brief_for("Trust is earned slowly.", None, "")
    assert b.is_abstract and "shaking hands" in b.visual_concept
    assert "illustrative" in b.planning_reason


def test_statistics_get_neutral_data_visual():
    b = brief_for("Sales grew 40% in 2025.", None, "")
    assert b.has_statistic and "chart" in b.visual_concept


def test_named_people_and_brands_detected_and_stripped_from_queries():
    b = brief_for("Last week Elon Musk said Tesla will build a new factory.", None, "")
    assert any("Elon Musk" in e for e in b.named_entities)
    assert "musk" not in b.primary_query.lower() and "tesla" not in b.primary_query.lower()
    assert "logo" in b.must_avoid


def test_sad_news_is_flagged_and_made_respectful():
    b = brief_for("Twelve people died when floods hit the village, and families are in mourning.", None, "")
    assert b.sensitivity == "tragic"
    assert "candles" in b.visual_concept
    assert {"party", "smiling", "blood"} <= set(b.must_avoid)


def test_visual_hint_wins():
    b = brief_for("Our app saves you time.", "phone screen showing a calendar app", "")
    assert b.primary_query == "phone screen showing a calendar app"


def test_avoid_expansion():
    terms = cp.expand_avoid(["people's faces", "alcohol"])
    assert {"face", "portrait", "beer", "wine"} <= set(terms)
    assert cp.avoid_faces(["people's faces"])


# ---------------------------------------------------------------- rights gate
def _cand(**kw) -> FootageCandidate:
    base = dict(provider="pixabay", asset_id="1", source_url="https://pixabay.com/videos/x-1/",
                licence=Licence(name="Pixabay Content License", url="https://pixabay.com/service/license-summary/",
                                commercial_use_allowed=True, evidence_status="provider_policy_v1"),
                attribution=Attribution(text="Video by a from Pixabay"))
    base.update(kw)
    return FootageCandidate(**base)


def test_rights_gate_accepts_complete_record():
    assert rights_service.check(_cand()) == []
    assert rights_service.completeness(_cand()) == 1.0


@pytest.mark.parametrize("kw,reason", [
    ({"licence": Licence()}, "licence evidence missing"),
    ({"licence": Licence(name="x", url="y", commercial_use_allowed=False, evidence_status="provider_policy_v1")}, "commercial"),
    ({"licence": Licence(name="x", url="y", commercial_use_allowed=True)}, "evidence status"),
    ({"watermarked": True}, "watermarked"),
    ({"editorial_only": True}, "editorial"),
    ({"provider": "random-scraper"}, "allowlist"),
    ({"source_url": None}, "source link"),
    ({"kind": "generated", "is_ai_generated": False}, "AI-generated"),
    ({"attribution": Attribution(required=True)}, "attribution required"),
])
def test_rights_gate_rejects(kw, reason):
    reasons = rights_service.check(_cand(**kw))
    assert any(reason.lower() in r.lower() for r in reasons), reasons


# ---------------------------------------------------------------- SSRF protection
@pytest.fixture
def fake_dns(monkeypatch):
    table = {"public.example": "93.184.216.34", "internal.example": "10.0.0.5", "meta.example": "169.254.169.254",
             "loop.example": "127.0.0.1", "v6loop.example": "::1"}

    def resolve(host, port, proto=0):
        if host not in table:
            raise socket.gaierror()
        fam = socket.AF_INET6 if ":" in table[host] else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (table[host], port))]

    monkeypatch.setattr(urls, "_resolver", resolve)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://public.example/a.mp3", "http://internal.example/a.mp3", "http://meta.example/latest",
    "http://loop.example/", "http://v6loop.example/", "http://user:pw@public.example/a.mp3", "http://nohost.example/",
])
def test_unsafe_urls_rejected(fake_dns, url):
    with pytest.raises(AppError) as ei:
        urls.check_public_url(url)
    assert ei.value.code == ErrorCode.UNSAFE_URL


def test_redirect_to_private_ip_is_blocked(fake_dns, tmp_path):
    def handler(req: httpx.Request):
        return httpx.Response(302, headers={"location": "http://internal.example/secret"})

    with pytest.raises(AppError) as ei:
        urls.safe_download("http://public.example/a.mp3", tmp_path / "x", 10_000, 5, transport=httpx.MockTransport(handler))
    assert ei.value.code == ErrorCode.UNSAFE_URL


def test_download_size_limit(fake_dns, tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(AppError) as ei:
        urls.safe_download("http://public.example/a.mp3", tmp_path / "x", 1000, 5, transport=transport)
    assert ei.value.code == ErrorCode.FILE_TOO_LARGE


# ---------------------------------------------------------------- signatures & rate limit
def test_hmac_signature_roundtrip():
    sig = sign_body("s3cret", b'{"a":1}')
    assert len(sig) == 64 and verify_signature("s3cret", b'{"a":1}', sig)
    assert not verify_signature("s3cret", b'{"a":2}', sig)


def test_rate_limiter():
    rl = RateLimiter(2)
    assert rl.check("ip") is None and rl.check("ip") is None
    assert rl.check("ip") is not None
    assert rl.check("other") is None


# ---------------------------------------------------------------- idempotency (DB level)
def test_idempotency_same_body_same_job_and_conflict():
    j1, c1 = repo.create_or_get_job(idempotency_key="k-unit-1", request_json={"script": "a"})
    j2, c2 = repo.create_or_get_job(idempotency_key="k-unit-1", request_json={"script": "a"})
    assert c1 and not c2 and j1.id == j2.id
    with pytest.raises(AppError) as ei:
        repo.create_or_get_job(idempotency_key="k-unit-1", request_json={"script": "b"})
    assert ei.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


def test_idempotency_under_concurrency():
    results, errors = [], []

    def go():
        try:
            results.append(repo.create_or_get_job(idempotency_key="k-race", request_json={"script": "same"}))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len({j.id for j, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


def test_progress_is_monotonic():
    j, _ = repo.create_or_get_job(idempotency_key="k-prog", request_json={"script": "p"})
    repo.update_job(j.id, progress=40)
    repo.update_job(j.id, progress=20)
    assert repo.get_job(j.id).progress == 40


# ---------------------------------------------------------------- script alignment
def test_script_alignment_keeps_script_spelling_with_asr_timing():
    asr = Transcript(duration_s=4.0, words=[
        Word(text="sales", start_s=0.2, end_s=0.5), Word(text="grew", start_s=0.6, end_s=0.9),
        Word(text="forty", start_s=1.0, end_s=1.3), Word(text="percent", start_s=1.35, end_s=1.8),
        Word(text="in", start_s=1.9, end_s=2.0), Word(text="twenty", start_s=2.1, end_s=2.4),
        Word(text="twenty-five", start_s=2.45, end_s=3.0)])
    t, ratio = align_script("Sales grew 40% in 2025.".split(), asr)
    assert [w.text for w in t.words] == ["Sales", "grew", "40%", "in", "2025."]
    assert t.words[0].start_s == 0.2 and t.words[2].start_s == pytest.approx(1.0)
    assert all(a.start_s <= b.start_s for a, b in zip(t.words, t.words[1:]))
    assert 0 < ratio < 1


# ---------------------------------------------------------------- input audio validation (real ffprobe)
def test_audio_formats_accepted(tmp_path):
    for ext in ("mp3", "wav", "m4a"):
        p = media.speechlike(tmp_path / f"a.{ext}", 2)
        assert validate_audio_file(p, f"a.{ext}")["format"] == ext


def test_wrong_file_type_rejected(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    with pytest.raises(AppError) as ei:
        validate_audio_file(txt, "notes.txt")
    assert ei.value.code == ErrorCode.UNSUPPORTED_AUDIO_FORMAT
    fake = tmp_path / "fake.mp3"
    fake.write_bytes(b"\x00" * 2000)
    with pytest.raises(AppError) as ei:
        validate_audio_file(fake, "fake.mp3")
    assert ei.value.code == ErrorCode.UNSUPPORTED_AUDIO_FORMAT
    vid = media.tiny_video(tmp_path / "clip.mp3")  # video renamed to .mp3
    with pytest.raises(AppError):
        validate_audio_file(vid, "clip.mp3")


def test_ten_minute_boundary(tmp_path):
    ok = media.speechlike(tmp_path / "ok.mp3", 599.5, rate=8000)
    assert validate_audio_file(ok, "ok.mp3")["duration_s"] < 600.5
    long = media.speechlike(tmp_path / "long.mp3", 605, rate=8000)
    with pytest.raises(AppError) as ei:
        validate_audio_file(long, "long.mp3")
    assert ei.value.code == ErrorCode.AUDIO_TOO_LONG


def test_silent_audio_detected(tmp_path):
    from app.services.input_service import speech_check

    p = media.silence(tmp_path / "s.wav", 5)
    with pytest.raises(AppError) as ei:
        speech_check(p, 5)
    assert ei.value.code == ErrorCode.NO_SPEECH_DETECTED


def test_music_only_detected_with_vad(tmp_path, monkeypatch):
    pytest.importorskip("faster_whisper")  # Silero VAD ships with faster-whisper (in the Docker image)
    from app.core.config import get_settings
    from app.services.input_service import speech_check

    monkeypatch.setattr(get_settings(), "asr_provider", "faster_whisper")
    p = media.music(tmp_path / "m.wav", 8)
    with pytest.raises(AppError) as ei:
        speech_check(p, 8)
    assert ei.value.code == ErrorCode.NO_SPEECH_DETECTED


def test_hosted_planner_failure_falls_back_to_rules():
    """A Gemini outage (e.g. 503 -> SCENE_PLANNING_FAILED) must not fail the job."""
    from app.adapters.llm import plan_with_fallback
    from app.adapters.llm.base import PlanRequest, SpanInfo
    from app.core.errors import AppError, ErrorCode

    class DownPlanner:
        name, model = "gemini", "test"

        def plan(self, req):
            raise AppError(ErrorCode.SCENE_PLANNING_FAILED, details={"reason": "ServerError"})

    req = PlanRequest(spans=[SpanInfo(0, "Cloud computing lets a small team rent servers.", 3.0),
                             SpanInfo(1, "Sales grew 40% in 2025.", 2.5)],
                      style="", avoid=[], language="en", target_scene_s=3.0, max_scene_s=8.0)
    plan, used, reason = plan_with_fallback(DownPlanner(), req)
    assert used.name == "rules" and reason == "ServerError"
    assert sorted(i for sc in plan.scenes for i in sc.span_indices) == [0, 1]
