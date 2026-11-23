import json
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import ERROR_CATALOGUE, AppError, ErrorCode
from app.core.logging import JsonFormatter, correlation_id_var, job_id_var
from app.main import create_app
from app.schemas.requests import BrollJobRequest

ROOT = Path(__file__).resolve().parents[2]


# ---------- configuration (Integration Standard #6) ----------
def test_env_example_lists_every_setting():
    env_keys = {
        line.split("=", 1)[0].strip()
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    setting_keys = {name.upper() for name in Settings.model_fields}
    assert setting_keys - env_keys == set(), "settings missing from .env.example"
    assert env_keys - setting_keys == set(), ".env.example has keys the app never reads"


# ---------- errors (Integration Standard #5) ----------
def test_every_error_code_is_catalogued_and_upper_snake():
    assert set(ERROR_CATALOGUE) == set(ErrorCode)
    for code in ErrorCode:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", code.value)


def test_error_envelope_shape():
    body = AppError(ErrorCode.EMPTY_SCRIPT, details={"x": 1}).to_dict()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "retryable", "details"}
    assert body["error"]["code"] == "EMPTY_SCRIPT" and body["error"]["retryable"] is False


def test_unknown_route_uses_standard_error(client: TestClient):
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


# ---------- health / ready / correlation id (Integration Standard #8) ----------
def test_health(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_ready_ok(client: TestClient):
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "queue": "ok", "storage": "ok"}


def test_ready_reports_failed_dependency():
    def broken():
        raise ConnectionError("down")

    c = TestClient(create_app(readiness_checks={"database": lambda: None, "storage": broken}))
    r = c.get("/ready")
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "SERVICE_NOT_READY" and err["retryable"] is True
    assert err["details"]["checks"]["storage"].startswith("error")


def test_correlation_id_echoed_and_generated(client: TestClient):
    assert client.get("/health", headers={"X-Correlation-Id": "abc-123"}).headers["X-Correlation-Id"] == "abc-123"
    assert client.get("/health").headers["X-Correlation-Id"]


def test_json_log_has_required_fields():
    tok1, tok2 = correlation_id_var.set("cid-1"), job_id_var.set("broll_x")
    try:
        rec = logging.makeLogRecord({"msg": "hello", "levelname": "INFO", "api_key": "sk-secret"})
        out = json.loads(JsonFormatter().format(rec))
    finally:
        correlation_id_var.reset(tok1)
        job_id_var.reset(tok2)
    assert {"timestamp", "level", "job_id", "correlation_id"} <= set(out)
    assert out["job_id"] == "broll_x" and out["correlation_id"] == "cid-1"
    assert out["api_key"] == "[REDACTED]"


# ---------- request schema ----------
@pytest.mark.parametrize(
    "body,code",
    [
        ({}, ErrorCode.MISSING_INPUT),
        ({"script": "hi", "audio_url": "https://example.com/a.mp3"}, ErrorCode.MULTIPLE_INPUTS),
        ({"script": "   "}, ErrorCode.EMPTY_SCRIPT),
        ({"scenes": [{"text": " "}]}, ErrorCode.EMPTY_SCRIPT),
        ({"script": "hi", "aspect_ratios": ["4:3"]}, ErrorCode.INVALID_ASPECT_RATIO),
    ],
)
def test_request_validation_errors(body, code):
    with pytest.raises(AppError) as ei:
        BrollJobRequest.model_validate(body)
    assert ei.value.code == code


def test_request_defaults_and_kinds():
    r = BrollJobRequest.model_validate({"script": "Hello world.", "aspect_ratios": ["9:16", "9:16", "1:1"]})
    assert r.input_kind == "script" and r.aspect_ratios == ["9:16", "1:1"]
    assert r.captions.burn_in and r.captions.srt
    s = BrollJobRequest.model_validate({"scenes": [{"text": "A bank by the river", "visual_hint": "riverbank"}]})
    assert s.input_kind == "scenes"


def test_gemini_tts_decodes_wav_and_raw_pcm():
    import base64
    import io
    import wave

    from app.adapters.tts.gemini_tts import _decode

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x01\x00" * 240)
    pcm, rate = _decode(base64.b64encode(buf.getvalue()).decode())
    assert rate == 24000 and len(pcm) == 480
    raw, rate2 = _decode(base64.b64encode(b"\x02\x00" * 10).decode())
    assert rate2 == 24000 and len(raw) == 20
