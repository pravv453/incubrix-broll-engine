"""Test environment: SQLite + local storage + inline job runner + mock providers.
No network, no API keys, no Docker services needed (only ffmpeg)."""
import os
import shutil
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="broll-tests-"))
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_TMP}/test.db",
    "STORAGE_BACKEND": "local",
    "LOCAL_STORAGE_DIR": str(_TMP / "storage"),
    "WORK_DIR": str(_TMP / "work"),
    "JOB_RUNNER": "inline",
    "ASR_PROVIDER": "mock", "TTS_PROVIDER": "mock", "LLM_PROVIDER": "rules", "EMBEDDING_PROVIDER": "mock",
    "VISION_PROVIDER": "mock", "FACE_DETECTOR": "mock", "JUDGE_PROVIDER": "mock",
    "FOOTAGE_PROVIDERS": "mock", "GENERATED_PROVIDER": "mock",
    "FAULT_INJECTION_FILE": str(_TMP / "faults.json"),
    "RATE_LIMIT_PER_MINUTE": "0", "DEMO_ACCESS_TOKEN": "", "GEMINI_API_KEY": "", "PIXABAY_API_KEY": "",
    "CALLBACK_HMAC_SECRET": "test-secret", "VIDEO_PRESET": "ultrafast",
})
for k in ("FFMPEG_BIN", "FFPROBE_BIN"):
    os.environ.setdefault(k, shutil.which(k.split("_")[0].lower()) or k.split("_")[0].lower())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

FAULTS = _TMP / "faults.json"


@pytest.fixture
def client() -> TestClient:
    """App with all readiness checks passing (no real dependencies)."""
    app = create_app(readiness_checks={"database": lambda: None, "queue": lambda: None, "storage": lambda: None})
    return TestClient(app)


@pytest.fixture
def faults():
    """Write provider faults for the duration of a test."""
    import json

    def set_(d: dict):
        FAULTS.write_text(json.dumps(d))

    yield set_
    FAULTS.unlink(missing_ok=True)


def wait_for(client: TestClient, job_id: str, timeout: float = 240) -> dict:
    """Poll until terminal; asserts progress never goes backwards."""
    last, seen = -1, []
    t0 = time.time()
    while time.time() - t0 < timeout:
        body = client.get(f"/api/v1/jobs/{job_id}").json()
        assert body["progress"] >= last, f"progress went backwards: {last} -> {body['progress']}"
        last = body["progress"]
        seen.append((body["stage"], body["progress"]))
        if body["status"] in ("succeeded", "failed", "cancelled"):
            body["_progress_trace"] = seen
            return body
        time.sleep(0.3)
    raise AssertionError(f"job {job_id} did not finish: {seen[-3:]}")
