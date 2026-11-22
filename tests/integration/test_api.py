"""HTTP contract: errors, idempotency, concurrency, auth, rate limit, OpenAPI parity."""
import json
import threading
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from tests.fixtures import media

ROOT = Path(__file__).resolve().parents[2]
H = {"Idempotency-Key": "x"}


def post(client, body, key):
    return client.post("/api/v1/broll/jobs", headers={"Idempotency-Key": key}, json=body)


def assert_error(r, status, code):
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code
    assert set(err) == {"code", "message", "retryable", "details"}


@pytest.mark.parametrize("body,status,code", [
    ({}, 422, "MISSING_INPUT"),
    ({"script": "a", "audio_url": "https://example.com/a.mp3"}, 422, "MULTIPLE_INPUTS"),
    ({"script": "   "}, 422, "EMPTY_SCRIPT"),
    ({"script": "hi", "aspect_ratios": ["4:3"]}, 422, "INVALID_ASPECT_RATIO"),
    ({"script": "hi", "unknown_field": 1}, 422, "INVALID_REQUEST"),
    ({"audio_url": "file:///etc/passwd"}, 422, "INVALID_REQUEST"),
    ({"audio_url": "http://127.0.0.1/a.mp3"}, 422, "UNSAFE_URL"),
    ({"audio_url": "http://169.254.169.254/latest/meta-data"}, 422, "UNSAFE_URL"),
    ({"script": "x" * 10001}, 422, "SCRIPT_TOO_LONG"),
])
def test_validation_errors(client, body, status, code):
    assert_error(post(client, body, f"v-{code}-{len(json.dumps(body))}"), status, code)


def test_idempotency_key_required(client):
    assert_error(client.post("/api/v1/broll/jobs", json={"script": "Hello."}), 400, "MISSING_IDEMPOTENCY_KEY")


def test_unknown_job(client):
    assert_error(client.get("/api/v1/jobs/broll_nope"), 404, "JOB_NOT_FOUND")


def test_wrong_upload_type(client, tmp_path):
    p = tmp_path / "doc.txt"
    p.write_text("not audio")
    r = client.post("/api/v1/broll/jobs", headers={"Idempotency-Key": "up-bad"},
                    files={"audio_file": ("doc.txt", p.read_bytes(), "text/plain")})
    assert_error(r, 415, "UNSUPPORTED_AUDIO_FORMAT")


def test_silent_upload_is_accepted_then_fails_clearly(client, tmp_path):
    from tests.conftest import wait_for

    p = media.silence(tmp_path / "silent.wav", 4)
    r = client.post("/api/v1/broll/jobs", headers={"Idempotency-Key": "up-silent"},
                    files={"audio_file": ("silent.wav", p.read_bytes(), "audio/wav")})
    assert r.status_code == 202
    body = wait_for(client, r.json()["job_id"])
    assert body["status"] == "failed"
    assert body["error"]["code"] == "NO_SPEECH_DETECTED" and body["error"]["retryable"] is False


def test_multipart_options_reject_text_inputs(client, tmp_path):
    p = media.speechlike(tmp_path / "a.wav", 2)
    r = client.post("/api/v1/broll/jobs", headers={"Idempotency-Key": "up-opt"},
                    files={"audio_file": ("a.wav", p.read_bytes(), "audio/wav")},
                    data={"options": json.dumps({"script": "no"})})
    assert_error(r, 422, "INVALID_REQUEST")


def test_idempotent_replay_and_conflict(client):
    body = {"script": "Idempotency check sentence one. And sentence two.", "aspect_ratios": ["1:1"]}
    r1 = post(client, body, "idem-1")
    r2 = post(client, body, "idem-1")
    assert r1.status_code == r2.status_code == 202
    assert r1.json()["job_id"] == r2.json()["job_id"]
    assert r1.headers["Idempotent-Replayed"] == "false" and r2.headers["Idempotent-Replayed"] == "true"
    assert_error(post(client, {**body, "style": "different"}, "idem-1"), 409, "IDEMPOTENCY_CONFLICT")


def test_concurrent_duplicate_posts_create_one_job(client, monkeypatch):
    calls = []
    monkeypatch.setattr("app.api.routes.broll.enqueue", lambda jid: calls.append(jid))
    ids, body = [], {"script": "Two users press submit at the same moment."}

    def go():
        ids.append(post(client, body, "race-key").json()["job_id"])

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(ids)) == 1 and len(calls) == 1, "work must be enqueued exactly once"


def test_cancel_queued_job(client, monkeypatch):
    monkeypatch.setattr("app.api.routes.broll.enqueue", lambda jid: None)
    jid = post(client, {"script": "Cancel me please."}, "cancel-1").json()["job_id"]
    r = client.post(f"/api/v1/jobs/{jid}/cancel", headers={"Idempotency-Key": "c1"})
    assert r.status_code == 202 and r.json()["status"] == "cancelled"
    again = client.post(f"/api/v1/jobs/{jid}/cancel", headers={"Idempotency-Key": "c1"})
    assert again.status_code == 202 and again.json()["status"] == "cancelled"


def test_timeline_not_ready(client, monkeypatch):
    monkeypatch.setattr("app.api.routes.broll.enqueue", lambda jid: None)
    jid = post(client, {"script": "Not ready yet."}, "nr-1").json()["job_id"]
    assert_error(client.get(f"/api/v1/jobs/{jid}/timeline"), 409, "JOB_NOT_READY")


def test_access_token_and_rate_limit(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "demo_access_token", "tok")
    monkeypatch.setattr(s, "rate_limit_per_minute", 2)
    c = TestClient(create_app(readiness_checks={}))
    assert_error(c.get("/api/v1/jobs/x"), 401, "UNAUTHORIZED")
    assert c.get("/health").status_code == 200  # probes stay open
    auth = {"Authorization": "Bearer tok"}
    monkeypatch.setattr("app.api.routes.broll.enqueue", lambda jid: None)
    for i in range(2):
        assert c.post("/api/v1/broll/jobs", headers={**auth, "Idempotency-Key": f"rl{i}"}, json={"script": "Hi."}).status_code == 202
    r = c.post("/api/v1/broll/jobs", headers={**auth, "Idempotency-Key": "rl9"}, json={"script": "Hi."})
    assert_error(r, 429, "RATE_LIMITED")
    assert "Retry-After" in r.headers


def test_openapi_yaml_matches_running_app(client):
    """Integration Standard #2: the checked-in contract is exactly the live one."""
    live = client.get("/openapi.json").json()
    assert live["openapi"].startswith("3.1")
    checked_in = yaml.safe_load((ROOT / "openapi.yaml").read_text())
    assert checked_in == live, "openapi.yaml is stale: run `python scripts/export_openapi.py`"
    assert all(p.startswith("/api/v1/") or p in ("/health", "/ready") for p in live["paths"])


def test_readme_lists_every_error_code():
    from app.core.errors import ErrorCode

    readme = (ROOT / "README.md").read_text()
    missing = [c.value for c in ErrorCode if f"`{c.value}`" not in readme]
    assert not missing, f"README error table is missing: {missing}"
