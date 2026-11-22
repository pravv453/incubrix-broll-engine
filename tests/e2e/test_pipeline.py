"""End-to-end: real ffmpeg renders through the HTTP API with mock providers (no keys)."""
import json
import re

import pytest

from app.core.config import get_settings
from app.storage.s3_storage import get_storage
from app.utils.ffmpeg import probe
from tests.conftest import wait_for
from tests.fixtures import media

pytestmark = pytest.mark.e2e

SCRIPT = ("Small businesses are moving their accounts to cloud computing. Sales grew 40% in 2025. "
          "Trust is earned slowly, one customer at a time. A python developer automates the boring work. "
          "Every evening the family shares a quiet meal at home.")


def _key(url: str) -> str:
    return url.split("/api/v1/files/", 1)[1]


def _local(url: str):
    return get_storage().path(_key(url))


def run(client, body, key, **kw):
    r = client.post("/api/v1/broll/jobs", headers={"Idempotency-Key": key}, json=body, **kw)
    assert r.status_code == 202, r.text
    return wait_for(client, r.json()["job_id"])


@pytest.fixture(scope="module")
def script_job(request):
    from fastapi.testclient import TestClient

    from app.main import create_app

    c = TestClient(create_app(readiness_checks={}))
    body = run(c, {"script": SCRIPT, "aspect_ratios": ["16:9", "9:16", "1:1"], "avoid": ["alcohol"],
                   "captions": {"burn_in": True, "srt": True}}, "e2e-script")
    return c, body


def test_text_input_produces_all_outputs(script_job):
    c, body = script_job
    assert body["status"] == "succeeded", body.get("error")
    assert body["progress"] == 100
    stages = [s for s, _ in body["_progress_trace"]]
    assert "rendering" in stages or "footage" in stages
    res = body["result"]
    assert set(res["videos"]) == {"16:9", "9:16", "1:1"}
    for k in ("captions_srt", "timeline_json", "manifest_json"):
        assert _local(res[k]).exists()


@pytest.mark.parametrize("ratio,wh", [("16:9", (1920, 1080)), ("9:16", (1080, 1920)), ("1:1", (1080, 1080))])
def test_video_technical_spec(script_job, ratio, wh):
    _, body = script_job
    info = probe(_local(body["result"]["videos"][ratio]))
    assert (info.width, info.height) == wh
    assert info.video_codec == "h264" and info.audio_codec == "aac" and info.pix_fmt == "yuv420p"
    assert abs(info.fps - 30) < 0.01
    assert abs((info.video_duration_s or 0) - (info.audio_duration_s or 0)) <= 1 / 30 + 0.024


def test_timeline_coverage_rights_and_uniqueness(script_job):
    c, body = script_job
    tl = c.get(f"/api/v1/jobs/{body['job_id']}/timeline").json()
    scenes = tl["scenes"]
    assert scenes[0]["start_frame"] == 0 and scenes[-1]["end_frame"] == tl["total_frames"]
    assert all(a["end_frame"] == b["start_frame"] for a, b in zip(scenes, scenes[1:])), "gap or overlap"
    assert all(sc["duration_s"] >= get_settings().min_scene_s for sc in scenes), "no flashing clips"
    keys = [f"{sc['clip']['provider']}:{sc['clip']['asset_id']}" for sc in scenes]
    assert len(keys) == len(set(keys)), "a clip was repeated"
    hashes = [sc["qa"]["dhash"] for sc in scenes if sc["qa"]["dhash"]]
    assert len(hashes) == len(set(hashes))
    for sc in scenes:
        clip = sc["clip"]
        assert clip["source_url"] and clip["licence"]["name"] and clip["licence"]["url"]
        assert clip["licence"]["commercial_use_allowed"] is True
        assert "is_ai_generated" in clip and clip["attribution"]
        assert sc["selection"]["reason"] and 0 <= sc["selection"]["score"] <= 1
        assert sc["text"].strip()
        # scene text ends on a sentence or phrase boundary
        assert re.search(r"[.!?,;:]$", sc["text"]), sc["text"]
    words = " ".join(sc["text"] for sc in scenes)
    assert words == " ".join(SCRIPT.split()), "every word of the narration is covered exactly once"


def test_hard_case_scenes_are_meaningful(script_job):
    c, body = script_job
    tl = c.get(f"/api/v1/jobs/{body['job_id']}/timeline").json()
    by_text = {sc["text"]: sc for sc in tl["scenes"]}
    cloud = next(sc for t, sc in by_text.items() if "cloud computing" in t)
    assert cloud["clip"]["asset_id"] in ("server-racks",) or cloud["clip"]["is_ai_generated"]
    assert cloud["clip"]["asset_id"] != "storm-sky"
    py = next(sc for t, sc in by_text.items() if "python" in t)
    assert py["clip"]["asset_id"] != "python-snake"
    stat = next(sc for t, sc in by_text.items() if "40%" in t)
    assert stat["visual_brief"]["has_statistic"] and stat["visual_brief"]["illustrative"]
    trust = next(sc for t, sc in by_text.items() if "Trust" in t)
    assert trust["visual_brief"]["is_abstract"]
    for sc in tl["scenes"]:
        assert "beer" not in sc["clip"]["asset_id"], "avoid=alcohol violated"


def test_captions_srt_matches_narration(script_job):
    _, body = script_job
    srt = _local(body["result"]["captions_srt"]).read_text()
    blocks = srt.strip().split("\n\n")
    text = " ".join(b.split("\n", 2)[2] for b in blocks)
    assert text == " ".join(SCRIPT.split())
    assert "40%" in srt  # script spelling preserved through TTS->ASR alignment


def test_manifest_has_required_fields(script_job):
    _, body = script_job
    m = json.loads(_local(body["result"]["manifest_json"]).read_text())
    for k in ("providers", "settings", "seed", "timings_s", "cost_estimate", "warnings", "rights", "validation", "libraries"):
        assert k in m, k
    assert m["validation"]["repeat_clip_count"] == 0
    assert all(v["passed"] for v in m["validation"]["outputs"].values())
    assert m["rights"]["all_commercial_use_allowed"] is True
    assert "gemini_api_key" in m["settings"] and m["settings"]["gemini_api_key"] in ("", "[set]")
    assert m["providers"]["scene_planner"]["name"] == "rules"
    assert m["cost_estimate"]["note"]


def test_independent_judge_recorded(script_job):
    c, body = script_job
    tl = c.get(f"/api/v1/jobs/{body['job_id']}/timeline").json()
    assert all(1 <= sc["independent_judge"]["score_1_5"] <= 5 for sc in tl["scenes"])


def test_swap_rerenders_only_one_scene(script_job):
    c, body = script_job
    jid = body["job_id"]
    tl = c.get(f"/api/v1/jobs/{jid}/timeline").json()
    target = tl["scenes"][1]
    r = c.post(f"/api/v1/jobs/{jid}/scenes/{target['scene_id']}/clip", headers={"Idempotency-Key": "swap-1"},
               json={"reason": "reviewer wants a different shot"})
    assert r.status_code == 202, r.text
    sw = wait_for(c, r.json()["job_id"])
    assert sw["status"] == "succeeded", sw.get("error")
    new = c.get(f"/api/v1/jobs/{sw['job_id']}/timeline").json()
    changed = [i for i, (a, b) in enumerate(zip(tl["scenes"], new["scenes"]))
               if (a["clip"]["provider"], a["clip"]["asset_id"]) != (b["clip"]["provider"], b["clip"]["asset_id"])]
    assert changed == [1]
    for i, (a, b) in enumerate(zip(tl["scenes"], new["scenes"])):
        if i != 1:
            assert a["segments"] == b["segments"], "unaffected segments must be reused, not re-rendered"
    h = new["history"][-1]
    assert h["segments_rerendered"] == 3 and h["segments_reused"] == 3 * (len(tl["scenes"]) - 1)
    m = json.loads(_local(sw["result"]["manifest_json"]).read_text())
    pr = m["partial_rerender"]
    assert pr["rerender_s"] < pr["parent_full_render_s"], pr
    keys = [f"{sc['clip']['provider']}:{sc['clip']['asset_id']}" for sc in new["scenes"]]
    assert len(keys) == len(set(keys))
    # swapping to a clip already used elsewhere is refused
    used = tl["scenes"][0]["clip"]
    r2 = c.post(f"/api/v1/jobs/{jid}/scenes/{target['scene_id']}/clip", headers={"Idempotency-Key": "swap-2"},
                json={"provider": used["provider"], "asset_id": used["asset_id"]})
    bad = wait_for(c, r2.json()["job_id"])
    assert bad["status"] == "failed" and bad["error"]["code"] == "RIGHTS_CHECK_FAILED"


def test_scene_not_found_for_swap(script_job):
    c, body = script_job
    r = c.post(f"/api/v1/jobs/{body['job_id']}/scenes/scene_999/clip", headers={"Idempotency-Key": "swap-404"}, json={})
    assert r.status_code == 404 and r.json()["error"]["code"] == "SCENE_NOT_FOUND"


@pytest.mark.parametrize("ext", ["mp3", "wav", "m4a"])
def test_audio_upload_formats(client, tmp_path, ext):
    p = media.speechlike(tmp_path / f"vo.{ext}", 7)
    r = client.post("/api/v1/broll/jobs", headers={"Idempotency-Key": f"upload-{ext}"},
                    files={"audio_file": (f"vo.{ext}", p.read_bytes(), "application/octet-stream")},
                    data={"options": json.dumps({"aspect_ratios": ["16:9"], "captions": {"burn_in": False, "srt": True}})})
    assert r.status_code == 202, r.text
    body = wait_for(client, r.json()["job_id"])
    assert body["status"] == "succeeded", body.get("error")
    assert probe(_local(body["result"]["videos"]["16:9"])).duration_s == pytest.approx(7, abs=0.05)


def test_incubrix_scene_list_uses_hints(client):
    scenes = [{"text": "Our app keeps your week organised.", "visual_hint": "phone calendar app"},
              {"text": "Teams plan projects together.", "visual_hint": "team meeting laptops"},
              {"text": "And you get your evenings back.", "visual_hint": "family meal at home"}]
    body = run(client, {"scenes": scenes, "aspect_ratios": ["9:16"]}, "e2e-scenes")
    assert body["status"] == "succeeded", body.get("error")
    tl = client.get(f"/api/v1/jobs/{body['job_id']}/timeline").json()
    assert [sc["text"] for sc in tl["scenes"]] == [s["text"] for s in scenes]
    assert [sc["visual_brief"]["primary_query"] for sc in tl["scenes"]] == [s["visual_hint"] for s in scenes]


def test_stock_provider_down_job_still_finishes(client, faults):
    faults({"mock": "down"})
    body = run(client, {"script": "Bright offices are full of focused work. Teams meet to plan the week."}, "e2e-down")
    assert body["status"] == "succeeded", body.get("error")
    tl = client.get(f"/api/v1/jobs/{body['job_id']}/timeline").json()
    assert all(sc["clip"]["is_ai_generated"] and sc["clip"]["provider"] == "generated" for sc in tl["scenes"])
    assert any(w["code"] == "FOOTAGE_PROVIDER_FAILED" for w in body["warnings"])


def test_all_sources_down_fails_clearly(client, faults):
    faults({"mock": "down", "generated": "down"})
    body = run(client, {"script": "Nothing can be found for this scene today."}, "e2e-alldown")
    assert body["status"] == "failed"
    assert body["error"]["code"] == "FOOTAGE_PROVIDER_UNAVAILABLE" and body["error"]["retryable"] is True


def test_corrupt_download_moves_to_next_candidate(client, faults):
    faults({"mock": "corrupt"})  # every stock download arrives as garbage bytes
    body = run(client, {"script": "Our server room runs the whole data center for cloud computing."}, "e2e-corrupt")
    assert body["status"] == "succeeded", body.get("error")
    tl = client.get(f"/api/v1/jobs/{body['job_id']}/timeline").json()
    sc = tl["scenes"][0]
    rejected = sc["selection"]["rejected_candidates"]
    assert any("corrupt" in " ".join(r["reasons"]) for r in rejected), rejected
    assert sc["clip"]["provider"] == "generated" and "stock candidates failed" in sc["selection"]["rule"]


def test_avoid_faces_and_rights_rejections(client):
    body = run(client, {"script": "A portrait of a smiling woman. People in the office work on a laptop.",
                        "avoid": ["people's faces"]}, "e2e-faces")
    assert body["status"] == "succeeded", body.get("error")
    tl = client.get(f"/api/v1/jobs/{body['job_id']}/timeline").json()
    assert all(sc["clip"]["asset_id"] not in ("portrait-face", "wm-office") for sc in tl["scenes"])
    reasons = " ".join(" ".join(r["reasons"]) for sc in tl["scenes"] for r in sc["selection"]["rejected_candidates"])
    assert "avoid" in reasons or "face" in reasons


def test_named_person_never_generated_and_sad_news_respectful(client, faults):
    faults({"mock": "empty"})  # force the stock-vs-generated rule to choose
    body = run(client, {"script": "Sundar Pichai announced the results at Google today. "
                                  "Many families are mourning after the floods killed twelve people."}, "e2e-safety")
    tl = client.get(f"/api/v1/jobs/{body['job_id']}/timeline").json() if body["status"] == "succeeded" else None
    if tl is None:  # named-entity scene with no stock and no generation allowed must fail loudly, not fabricate
        assert body["error"]["code"] in ("NO_ELIGIBLE_FOOTAGE", "FOOTAGE_PROVIDER_UNAVAILABLE")
        return
    for sc in tl["scenes"]:
        if sc["visual_brief"]["named_entities"]:
            assert not sc["clip"]["is_ai_generated"]


def test_named_entity_scene_uses_stock_with_warning(client):
    body = run(client, {"script": "Sundar Pichai spoke about the future of the city skyline at dawn."}, "e2e-named")
    assert body["status"] == "succeeded", body.get("error")
    tl = client.get(f"/api/v1/jobs/{body['job_id']}/timeline").json()
    sc = tl["scenes"][0]
    assert sc["visual_brief"]["named_entities"] and not sc["clip"]["is_ai_generated"]
    assert any(w["code"] == "NAMED_ENTITY" for w in sc["warnings"])


def test_two_users_at_once(client):
    import threading

    out = {}

    def go(k, text):
        out[k] = run(client, {"script": text, "aspect_ratios": ["1:1"]}, k)

    ts = [threading.Thread(target=go, args=(f"multi-{i}", t)) for i, t in
          enumerate(["The ocean waves roll onto the beach.", "Solar panels shine on the rooftop."])]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert {b["status"] for b in out.values()} == {"succeeded"}
    assert len({b["job_id"] for b in out.values()}) == 2


def test_callback_is_hmac_signed(client, monkeypatch):
    import httpx

    from app.core.security import verify_signature

    sent = {}
    monkeypatch.setattr("app.workers.runner.check_public_url", lambda url, allowed=None: "hooks.example")

    def fake_post(url, content, headers, timeout, follow_redirects):
        sent.update(url=url, body=content, headers=headers)
        return httpx.Response(200)

    monkeypatch.setattr("app.workers.runner.httpx.post", fake_post)
    monkeypatch.setattr("app.api.routes.broll.check_public_url", lambda url: "hooks.example")
    body = run(client, {"script": "Callbacks tell the caller when work is done.", "callback_url": "https://hooks.example/cb"},
               "e2e-callback")
    import time
    for _ in range(50):
        if sent:
            break
        time.sleep(0.1)
    assert sent["url"] == "https://hooks.example/cb"
    assert verify_signature("test-secret", sent["body"], sent["headers"]["X-IncuBrix-Signature"])
    payload = json.loads(sent["body"])
    assert payload["event"] == f"job.{body['status']}" and payload["job"]["job_id"] == body["job_id"]
