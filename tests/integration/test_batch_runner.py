"""Integration Standard #10: the batch runner drives a live HTTP server and writes one results folder."""
import csv
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def live_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    from app.core.config import get_settings

    get_settings().public_base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(create_app(readiness_checks={}), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def test_batch_runner_folder(live_server, tmp_path):
    scen = tmp_path / "scen"
    scen.mkdir()
    (scen / "good.json").write_text(json.dumps({"name": "good", "request": {
        "script": "The ocean waves roll onto the beach. Solar panels shine on the rooftop.", "aspect_ratios": ["1:1"]}}))
    (scen / "bad.json").write_text(json.dumps({"name": "bad_empty", "request": {"script": "  "}}))
    out = tmp_path / "results"
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/batch_runner.py"), "--input", str(scen), "--api", live_server,
                           "--output", str(out), "--poll-interval", "0.5"], capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads((out / "summary.json").read_text())
    by = {r["scenario"]: r for r in summary["rows"]}
    assert by["good"]["status"] == "succeeded"
    assert by["bad_empty"]["status"] == "rejected" and by["bad_empty"]["error_code"] == "EMPTY_SCRIPT"
    for f in ("video_1x1.mp4", "captions.srt", "timeline.json", "manifest.json", "request.json", "final_status.json"):
        assert (out / "good" / f).exists(), f
    assert (out / "bad_empty" / "error.json").exists()
    rows = list(csv.DictReader(open(out / "ratings_template.csv")))
    assert rows and all(r["human_rating_1_5"] == "" for r in rows)
    assert (out / "index.md").exists() and (out / "summary.csv").exists()
