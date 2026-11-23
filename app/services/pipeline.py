"""Job orchestrator. One function per job kind; each stage is timed, checkpoints
for cancellation, and reports monotonic progress. Deterministic code owns
timing, rights, uniqueness and rendering; models only propose."""
import json
import logging
import platform
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from app.adapters.embeddings import get_embedder
from app.adapters.footage.base import ProviderError
from app.adapters.llm import get_planner, plan_with_fallback
from app.adapters.llm.base import PlanRequest, SpanInfo
from app.adapters.vision import get_vision
from app.adapters.vision.faces import get_face_detector
from app.adapters.vision.judge import get_judge
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db import repositories as repo
from app.schemas.scenes import SceneBrief
from app.schemas.transcript import Transcript
from app.services import content_policy as cp
from app.services import input_service as inp
from app.services import rights_service
from app.services.caption_service import build_cues, to_srt
from app.services.footage_service import FootageSelector, SceneNeed, Selection
from app.services.render_service import SegmentSpec, canvas, concat_and_mux, ratio_slug, render_segment
from app.services.segmentation import (
    SceneSpan, WordSpan, build_scene_spans, enforce_min_duration, split_sentences, validate_coverage,
)
from app.services.validation_service import validate_output
from app.storage.s3_storage import get_storage, sha256_file
from app.utils.urls import safe_download

log = logging.getLogger("app.pipeline")
TIMELINE_VERSION = "1.0"


class JobCancelled(Exception):
    pass


class Ctx:
    def __init__(self, job_id: str):
        self.s = get_settings()
        self.job_id = job_id
        self.job = repo.require_job(job_id)
        self.req = self.job.request_json
        self.storage = get_storage()
        self.work = Path(self.s.work_dir) / job_id
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True, exist_ok=True)
        self.prefix = f"jobs/{job_id}"
        self.timings: dict[str, float] = {}
        self.warnings: list[dict] = []
        self.usage: dict[str, float] = {"tts_characters": 0, "llm_calls": 0, "image_generations": 0,
                                        "judge_calls": 0, "vision_calls": 0, "stock_searches": 0}
        self.t0 = time.perf_counter()
        self.planner_fallback: str | None = None

    def progress(self, stage: str, pct: float) -> None:
        if repo.get_job(self.job_id).cancel_requested:
            raise JobCancelled()
        repo.update_job(self.job_id, stage=stage, progress=int(pct))

    def warn(self, code: str, message: str, **details) -> None:
        w = {"code": code, "message": message, **details}
        self.warnings.append(w)
        repo.add_warning(self.job_id, w)

    @contextmanager
    def stage(self, name: str, pct: float):
        self.progress(name, pct)
        t = time.perf_counter()
        try:
            yield
        finally:
            self.timings[name] = round(self.timings.get(name, 0) + time.perf_counter() - t, 3)

    def put(self, name: str, path: Path, content_type: str | None = None) -> str:
        return self.storage.put_file(f"{self.prefix}/{name}", path, content_type)

    def put_json(self, name: str, data) -> str:
        return self.storage.put_bytes(f"{self.prefix}/{name}", json.dumps(data, indent=2, ensure_ascii=False, default=str).encode(),
                                      "application/json")


# ============================================================== narration
def resolve_narration(ctx: Ctx) -> tuple[Path, float, str | None, dict]:
    """Returns (canonical wav, duration, known script text or None, input metadata)."""
    req, s = ctx.req, ctx.s
    wav = ctx.work / "narration.wav"
    if req.get("script") is not None or req.get("scenes") is not None:
        text = req["script"] if req.get("script") is not None else " ".join(x["text"].strip() for x in req["scenes"])
        text = " ".join(text.split())
        if not text:
            raise AppError(ErrorCode.EMPTY_SCRIPT)
        if len(text) > s.max_script_chars:
            raise AppError(ErrorCode.SCRIPT_TOO_LONG, details={"chars": len(text), "max_chars": s.max_script_chars})
        dur, meta = inp.generate_narration(text, req.get("voice", "en-IN-female"), req.get("language"), wav)
        ctx.usage["tts_characters"] += len(text)
        if dur > s.max_audio_duration_s:
            raise AppError(ErrorCode.AUDIO_TOO_LONG, details={"duration_s": dur, "reason": "generated narration too long"})
        return wav, dur, text, {"kind": "script" if req.get("script") is not None else "scenes", "tts": meta,
                                "characters": len(text)}
    src = ctx.work / "input_audio"
    if req.get("audio_url"):
        src, ctype = safe_download(req["audio_url"], src, s.max_upload_mb * 1024 * 1024, s.url_fetch_timeout_s)
        meta = {"kind": "audio_url", "url_host": req["audio_url"].split("/")[2], "content_type": ctype}
        name = req["audio_url"].split("?")[0].rsplit("/", 1)[-1]
    else:
        f = req["audio_file"]
        ctx.storage.get_file(f["key"], src)
        meta = {"kind": "audio_file", "filename": f.get("filename")}
        name = f.get("filename")
    info = inp.validate_audio_file(src, name if name and "." in name else None)
    meta.update(info, sha256=sha256_file(src))
    dur = inp.to_canonical_wav(src, wav)
    return wav, dur, None, meta


# ============================================================== planning
def scene_word_spans(scene_texts: list[str], n_words: int) -> list[WordSpan]:
    spans, i = [], 0
    for t in scene_texts:
        n = len(t.split())
        spans.append(WordSpan(i, i + n - 1))
        i += n
    assert i == n_words
    return spans


def apply_safety_layer(brief: SceneBrief, text: str, request_avoid: list[str]) -> SceneBrief:
    """Union rule-based safety flags with whatever the planner said."""
    sens_rank = {"none": 0, "sensitive": 1, "tragic": 2}
    rule_sens = cp.sensitivity(text)
    if sens_rank[rule_sens] > sens_rank[brief.sensitivity]:
        brief.sensitivity = rule_sens
    ents = list(dict.fromkeys(brief.named_entities + cp.named_entities(text)))
    brief.named_entities = ents
    if ents:
        brief.primary_query = cp.strip_entities(brief.primary_query, ents) or "people working office"
        brief.alternative_queries = [q for q in (cp.strip_entities(q, ents) for q in brief.alternative_queries) if q]
        brief.must_avoid = list(dict.fromkeys(brief.must_avoid + ["logo", "brand"]))
    if brief.sensitivity != "none":
        brief.must_avoid = list(dict.fromkeys(brief.must_avoid + cp.SENSITIVE_BLOCK))
    brief.has_statistic = brief.has_statistic or cp.has_statistic(text)
    return brief


def plan_scenes(ctx: Ctx, transcript: Transcript) -> list[tuple[SceneSpan, SceneBrief]]:
    s, req = ctx.s, ctx.req
    fps = s.output_fps
    words = transcript.words
    fixed = req.get("scenes") is not None
    if fixed:
        spans = scene_word_spans([x["text"] for x in req["scenes"]], len(words))
    else:
        spans = split_sentences(words, max_span_s=min(s.max_scene_s, s.target_scene_s * 1.5))
    infos = [SpanInfo(i, " ".join(w.text for w in words[sp.first: sp.last + 1]),
                      words[sp.last].end_s - words[sp.first].start_s) for i, sp in enumerate(spans)]
    planner = get_planner()
    preq = PlanRequest(spans=infos, style=req.get("style", ""), avoid=req.get("avoid", []),
                       language=transcript.language, target_scene_s=s.target_scene_s, max_scene_s=s.max_scene_s,
                       fixed_groups=[[i] for i in range(len(spans))] if fixed else None,
                       hints=[x.get("visual_hint") for x in req["scenes"]] if fixed else None)
    configured = planner.name
    plan, planner, failure = plan_with_fallback(planner, preq)
    ctx.usage["llm_calls"] += 1 if configured != "rules" else 0
    if failure is not None:
        ctx.planner_fallback = failure
        ctx.warn("PLANNER_FALLBACK", f"{configured} scene planner failed ({failure}); used the rules planner instead")
    groups = [b.span_indices for b in plan.scenes]
    raw = build_scene_spans(transcript, spans, groups, fps)
    briefs = plan.scenes
    if fixed:
        merged = raw
        for sc in raw:
            if sc.frames < round(s.min_scene_s * fps):
                ctx.warn("SHORT_SCENE_KEPT", "IncuBrix scene shorter than MIN_SCENE_S kept as supplied",
                         scene_index=sc.scene_index, duration_s=round(sc.frames / fps, 2))
    else:
        merged = enforce_min_duration(raw, fps, s.min_scene_s)
        if len(merged) < len(raw):
            ctx.warn("SHORT_SCENES_MERGED", f"{len(raw) - len(merged)} scene(s) shorter than {s.min_scene_s}s merged into neighbours")
    validate_coverage(merged, transcript, fps)
    out = []
    for sc in merged:
        # brief of the longest original scene inside this merged scene
        inside = [(r, b) for r, b in zip(raw, briefs) if r.first_word >= sc.first_word and r.last_word <= sc.last_word]
        r, b = max(inside, key=lambda rb: rb[0].frames)
        text = " ".join(w.text for w in words[sc.first_word: sc.last_word + 1])
        out.append((sc, apply_safety_layer(b.model_copy(deep=True), text, req.get("avoid", []))))
    return out


# ============================================================== shared render helpers
def spec_from_selection(scene_id: str, sel_like: dict, sc_frames: int, start_s: float, clip_path: Path) -> SegmentSpec:
    return SegmentSpec(scene_id=scene_id, clip_path=clip_path, src_w=sel_like["width"], src_h=sel_like["height"],
                       clip_duration_s=sel_like["clip_duration_s"], in_point_s=sel_like["in_point_s"], frames=sc_frames,
                       start_s=start_s, saliency_cx=sel_like["saliency_cx"], saliency_spread=sel_like["saliency_spread"],
                       luma=sel_like["luma"], saturation=sel_like["saturation"])


def sel_dict(sel: Selection) -> dict:
    return {"width": sel.width, "height": sel.height, "clip_duration_s": sel.clip_duration_s,
            "in_point_s": sel.in_point_s, "saliency_cx": sel.saliency_cx, "saliency_spread": sel.saliency_spread,
            "luma": sel.luma, "saturation": sel.saturation}


def clip_record(sel: Selection, clip_key: str | None) -> dict:
    c = sel.candidate
    return {
        "provider": c.provider, "asset_id": c.asset_id, "kind": c.kind, "title": c.title,
        "source_url": c.source_url, "downloaded_object": clip_key,
        "licence": c.licence.model_dump(), "attribution": c.attribution.model_dump(),
        "is_ai_generated": c.is_ai_generated, "generation_prompt": c.extra.get("prompt"),
        "retrieved_at": c.retrieved_at, "source_width": sel.width, "source_height": sel.height,
        "source_duration_s": round(sel.clip_duration_s, 3), "in_point_s": sel.in_point_s,
    }


def selection_record(sel: Selection, weights: dict, tau: float) -> dict:
    return {
        "score": sel.score, "relevance_score": sel.score, "score_components": sel.components, "weights": weights,
        "rank": sel.rank, "rule": sel.rule, "stock_min_score_tau": tau, "reason": sel.reason,
        "alternatives": sel.alternatives, "rejected_candidates": sel.rejected,
    }


def ffmpeg_version() -> str:
    try:
        out = subprocess.run([get_settings().ffmpeg_bin, "-version"], capture_output=True, text=True, timeout=10).stdout
        return out.splitlines()[0]
    except Exception:  # noqa: BLE001
        return "unknown"


def lib_versions() -> dict:
    out = {}
    for pkg in ("fastapi", "pydantic", "celery", "sqlalchemy", "boto3", "httpx", "google-genai", "faster-whisper",
                "fastembed", "opencv-python-headless"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            pass
    out["python"] = platform.python_version()
    out["ffmpeg"] = ffmpeg_version()
    return out


def cost_block(ctx: Ctx, finished_s: float) -> dict:
    s = ctx.s
    compute_h = (time.perf_counter() - ctx.t0) / 3600
    items = {
        "tts": (ctx.usage["tts_characters"] / 1000, s.cost("cost_tts_per_1k_chars"), "per 1k characters"),
        "llm": (ctx.usage["llm_calls"], s.cost("cost_llm_per_call"), "per planning call"),
        "image_generation": (ctx.usage["image_generations"], s.cost("cost_image_per_generation"), "per image"),
        "judge": (ctx.usage["judge_calls"], s.cost("cost_judge_per_call"), "per judged scene"),
        "compute": (compute_h, s.cost("cost_compute_per_hour"), "per worker hour"),
        "stock_footage": (ctx.usage["stock_searches"], 0.0 if s.footage_provider_list else None,
                          "Pixabay/mock API calls are free"),
    }
    out, total, unknown = {}, 0.0, []
    for k, (units, price, unit) in items.items():
        cost = None if price is None else round(units * price, 6)
        if units and cost is None:
            unknown.append(k)
        out[k] = {"units": round(units, 4), "unit": unit, "unit_price_usd": price, "cost_usd": cost}
        total += cost or 0.0
    minutes = finished_s / 60
    return {"items": out, "total_known_usd": round(total, 6), "unknown_items": unknown,
            "per_finished_minute_usd": round(total / minutes, 6) if minutes else None,
            "note": "Unit prices come from COST_* settings; blank = unknown, never guessed."}


def provider_block(ctx: Ctx, selector: FootageSelector | None, asr_meta: dict, tts_meta: dict | None) -> dict:
    s = ctx.s
    planner = get_planner()
    emb = get_embedder()
    vis = get_vision()
    faces = get_face_detector()
    judge = get_judge()
    gen = selector.generated if selector else None
    return {
        "tts": tts_meta, "asr": asr_meta,
        "scene_planner": {"name": planner.name, "model": planner.model,
                          **({"fallback_used": "rules", "fallback_reason": ctx.planner_fallback}
                             if getattr(ctx, "planner_fallback", None) else {})},
        "embeddings": {"name": emb.name, "model": emb.model},
        "vision_scorer": {"name": vis.name, "model": vis.model} if vis else None,
        "face_detector": {"name": faces.name, "model": s.face_model_path} if faces else None,
        "independent_judge": {"name": judge.name, "model": judge.model} if judge else None,
        "footage_stock": s.footage_provider_list,
        "footage_generated": {"name": "generated", "backend": gen.backend.name,
                              "model": getattr(gen.backend, "model", None)} if gen else None,
        "renderer": {"name": "ffmpeg", "version": ffmpeg_version(), "video": f"libx264 crf={s.video_crf} preset={s.video_preset} yuv420p {s.output_fps}fps",
                     "audio": f"aac {s.audio_bitrate} 48kHz"},
    }


def judge_scenes(ctx: Ctx, selector: FootageSelector, scenes: list[dict], selections: dict[str, Selection]) -> None:
    judge = get_judge()
    if not judge:
        return
    for sc in scenes:
        sel = selections.get(sc["scene_id"])
        if not sel:
            continue
        try:
            frames = selector.frames_for_judge(sel, sc["duration_s"], sc["scene_id"])
            res = judge.judge(sc["text"], frames)
            ctx.usage["judge_calls"] += 1
            sc["independent_judge"] = {"provider": judge.name, "model": judge.model, "score_1_5": res["score"],
                                       "rationale": res.get("rationale", ""), "safety_flags": res.get("safety_flags", []),
                                       "sampled_times_s": [round(sel.in_point_s + min(sc["duration_s"], sel.clip_duration_s - sel.in_point_s) * f, 2) for f in (0.2, 0.5, 0.8)]}
        except Exception as exc:  # noqa: BLE001
            ctx.warn("JUDGE_FAILED", f"independent judge failed: {type(exc).__name__}", scene_id=sc["scene_id"])


# ============================================================== main job
def run_broll(job_id: str) -> dict:
    ctx = Ctx(job_id)
    s, req = ctx.s, ctx.req
    fps = s.output_fps
    ratios = req.get("aspect_ratios", ["16:9"])
    caps = req.get("captions", {"burn_in": True, "srt": True})

    with ctx.stage("narration", 5):
        wav, duration, script_text, input_meta = resolve_narration(ctx)
        narration_key = ctx.put("work/narration.wav", wav, "audio/wav")

    with ctx.stage("transcription", 14):
        synthetic = bool((input_meta.get("tts") or {}).get("synthetic"))
        if synthetic:
            # Mock TTS emits a tone, not speech: real ASR/VAD would (correctly) find no words.
            # Timing comes from the mock's own speaking-rate model, and we say so.
            from app.adapters.asr.mock import MockASR

            speech = {"method": "skipped: synthetic mock narration"}
            asr_t = MockASR().transcribe(wav, duration, req.get("language"), script_text)
            asr_t.timing_source = "mock_tts_timing_model"
            ctx.warn("MOCK_NARRATION", "TTS_PROVIDER=mock: narration is a placeholder tone; set TTS_PROVIDER=gemini for real speech")
        else:
            speech = inp.speech_check(wav, duration)
            asr_t = inp.transcribe(wav, duration, req.get("language"), script_text)
        asr_meta = {"provider": asr_t.provider, "model": asr_t.model, "timing_source": asr_t.timing_source,
                    "detected_language": asr_t.language, "speech_check": speech, "asr_word_count": len(asr_t.words)}
        transcript = asr_t
        if script_text:
            transcript, ratio = inp.align_script(script_text.split(), asr_t)
            asr_meta["script_alignment_ratio"] = ratio
            if ratio < 0.6:
                ctx.warn("LOW_SCRIPT_ALIGNMENT", f"only {ratio:.0%} of script words matched the ASR of the narration; timings interpolated")
        ctx.put_json("work/transcript.json", transcript.model_dump())

    with ctx.stage("scene_planning", 25):
        planned = plan_scenes(ctx, transcript)

    selector = FootageSelector(ctx.work, ratios, req.get("style", ""), req.get("avoid", []), s.seed,
                               Path(s.work_dir) / "_cache")
    selections: dict[str, Selection] = {}
    scenes: list[dict] = []
    candidates_dump: dict[str, list] = {}
    words = transcript.words
    with ctx.stage("footage", 32):
        for i, (sc, brief) in enumerate(planned):
            sid = f"scene_{i + 1:03d}"
            text = " ".join(w.text for w in words[sc.first_word: sc.last_word + 1])
            need = SceneNeed(sid, text, sc.frames / fps, brief)
            sel = selector.select(need)
            selections[sid] = sel
            candidates_dump[sid] = [x.model_dump() for x in sel.ranked]
            for w in sel.warnings:
                ctx.warn(**w)
            scene_warn = []
            if brief.named_entities:
                scene_warn.append({"code": "NAMED_ENTITY", "message": f"mentions {brief.named_entities}: generic visuals used; no likeness or logo generated"})
            if brief.sensitivity != "none":
                scene_warn.append({"code": "SENSITIVE_TOPIC", "message": f"{brief.sensitivity} topic: calm, non-graphic footage enforced"})
            if brief.is_abstract or brief.has_statistic:
                scene_warn.append({"code": "ILLUSTRATIVE", "message": "abstract line or statistic: footage is illustrative, not literal evidence"})
            scenes.append({
                "scene_id": sid, "index": i, "text": text,
                "start_s": round(sc.start_frame / fps, 3), "end_s": round(sc.end_frame / fps, 3),
                "duration_s": round(sc.frames / fps, 3), "start_frame": sc.start_frame, "end_frame": sc.end_frame,
                "word_range": [sc.first_word, sc.last_word],
                "visual_brief": {**brief.model_dump(exclude={"span_indices"}),
                                 "illustrative": brief.is_abstract or brief.has_statistic},
                "clip": None, "selection": selection_record(sel, selector.weights, s.stock_min_score),
                "warnings": scene_warn,
            })
            ctx.progress("footage", 32 + 30 * (i + 1) / len(planned))
    ctx.usage["stock_searches"] = selector.counters["searches"]
    ctx.usage["image_generations"] = selector.counters["generations"]
    ctx.usage["vision_calls"] = selector.counters["vision_calls"]
    for f in selector.provider_failures:
        ctx.warn("FOOTAGE_PROVIDER_FAILED", f"{f['provider']}: {f['reason']}", provider=f["provider"], scene_id=f.get("scene_id"))

    with ctx.stage("captions", 63):
        cues = build_cues(words, s.caption_max_chars, s.caption_max_words)
        srt_path = ctx.work / "captions.srt"
        srt_path.write_text(to_srt(cues), encoding="utf-8")

    total_frames = scenes[-1]["end_frame"]
    outputs: dict = {"videos": {}, "segments": {}}
    validation: dict = {}
    render_t0 = time.perf_counter()
    with ctx.stage("rendering", 65):
        n_jobs = len(scenes) * len(ratios)
        done = 0
        for ratio in ratios:
            slug = ratio_slug(ratio)
            seg_dir = ctx.work / "segments" / slug
            seg_dir.mkdir(parents=True, exist_ok=True)
            seg_paths = []
            for sc in scenes:
                sel = selections[sc["scene_id"]]
                spec = spec_from_selection(sc["scene_id"], sel_dict(sel), sc["end_frame"] - sc["start_frame"], sc["start_s"], sel.clip_path)
                out = seg_dir / f"{sc['scene_id']}.mp4"
                info = render_segment(spec, ratio, out, cues if caps.get("burn_in") else None, transcript.language)
                sc.setdefault("render", {})[ratio] = info
                if info["looped"]:
                    sc["warnings"].append({"code": "CLIP_LOOPED", "message": f"source clip too short for {sc['duration_s']}s; looped"})
                seg_paths.append(out)
                done += 1
                ctx.progress("rendering", 65 + 25 * done / n_jobs)
            final = ctx.work / f"video_{slug}.mp4"
            concat_and_mux(seg_paths, wav, total_frames, final)
            W, H = canvas(ratio)
            validation[ratio] = validate_output(final, W, H, total_frames)
            outputs["segments"][ratio] = seg_paths
            outputs["videos"][ratio] = final
    render_s = round(time.perf_counter() - render_t0, 3)

    failed = {r: v for r, v in validation.items() if not v["passed"]}
    if failed:
        for r, v in failed.items():  # keep failed renders as evidence
            ctx.put(f"failed/video_{ratio_slug(r)}.mp4", outputs["videos"][r], "video/mp4")
        ctx.put_json("failed/validation.json", validation)
        raise AppError(ErrorCode.OUTPUT_VALIDATION_FAILED,
                       details={r: [k for k, ok in v["checks"].items() if not ok] for r, v in failed.items()})

    with ctx.stage("judging", 91):
        judge_scenes(ctx, selector, scenes, selections)

    with ctx.stage("uploading", 95):
        result: dict = {"videos": {}}
        for ratio, path in outputs["videos"].items():
            result["videos"][ratio] = ctx.put(f"video_{ratio_slug(ratio)}.mp4", path, "video/mp4")
        for sc in scenes:
            sel = selections[sc["scene_id"]]
            clip_key = ctx.put(f"clips/{sc['scene_id']}.mp4", sel.clip_path, "video/mp4")
            sc["clip"] = clip_record(sel, clip_key)
            sc["qa"] = {"source_luma": round(sel.luma, 3), "source_saturation": round(sel.saturation, 3),
                        "dhash": sel.dhash, "saliency_cx": round(sel.saliency_cx, 3),
                        "saliency_spread": round(sel.saliency_spread, 3), "black_free": True, "freeze_free": True}
            sc["segments"] = {r: ctx.put(f"segments/{ratio_slug(r)}/{sc['scene_id']}.mp4", p, "video/mp4")
                              for r in ratios for p in [ctx.work / "segments" / ratio_slug(r) / f"{sc['scene_id']}.mp4"]}
        if caps.get("srt", True):
            result["captions_srt"] = ctx.put("captions.srt", srt_path, "application/x-subrip")
        ctx.put_json("work/candidates.json", candidates_dump)
        timeline = {
            "timeline_version": TIMELINE_VERSION, "job_id": job_id, "input_kind": input_meta["kind"],
            "language": transcript.language, "fps": fps, "duration_s": round(duration, 3), "total_frames": total_frames,
            "aspect_ratios": ratios, "narration_object": narration_key, "transcript_object": f"{ctx.prefix}/work/transcript.json",
            "captions": {"burn_in": bool(caps.get("burn_in")), "srt": bool(caps.get("srt", True)), "cue_count": len(cues)},
            "style": req.get("style"), "avoid": req.get("avoid", []),
            "render_stats": {"full_render_s": render_s, "segments_rendered": len(scenes) * len(ratios)},
            "scenes": scenes, "history": [],
        }
        result["timeline_json"] = ctx.put_json("timeline.json", timeline)
        finished_s = duration
        manifest = build_manifest(ctx, timeline, selector, asr_meta, input_meta.get("tts"), input_meta, validation,
                                  result, finished_s, render_s)
        result["manifest_json"] = ctx.put_json("manifest.json", manifest)
    shutil.rmtree(ctx.work, ignore_errors=True)
    return result


def build_manifest(ctx: Ctx, timeline: dict, selector, asr_meta, tts_meta, input_meta, validation, result,
                   finished_s: float, render_s: float, extra: dict | None = None) -> dict:
    s = ctx.s
    scenes = timeline["scenes"]
    assets = [{"scene_id": sc["scene_id"], **{k: sc["clip"][k] for k in ("provider", "asset_id", "source_url", "licence",
               "attribution", "is_ai_generated", "retrieved_at")}} for sc in scenes]
    keys = [f"{a['provider']}:{a['asset_id']}" for a in assets]
    ctx.timings["total"] = round(time.perf_counter() - ctx.t0, 3)
    return {
        "manifest_version": "1.0",
        "job_id": ctx.job_id, "kind": ctx.job.kind, "parent_job_id": ctx.job.parent_job_id,
        "created_at": ctx.job.created_at.isoformat() if ctx.job.created_at else None,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "app": {"name": "incubrix-broll-engine", "version": s.app_version, "git_sha": s.git_sha},
        "input": {k: v for k, v in input_meta.items() if k != "tts"},
        "providers": provider_block(ctx, selector, asr_meta, tts_meta),
        "libraries": lib_versions(),
        "settings": s.public_dict(),
        "ranking": {"weights": selector.weights if selector else None, "stock_min_score_tau": s.stock_min_score,
                    "rule": "stock if best eligible stock score >= tau, else AI-generated (never for named people/brands)"},
        "seed": s.seed,
        "timings_s": {**ctx.timings, "render_only": render_s},
        "turnaround_s": ctx.timings["total"],
        "usage": ctx.usage,
        "cost_estimate": cost_block(ctx, finished_s),
        "warnings": ctx.warnings,
        "provider_failures": selector.provider_failures if selector else [],
        "rights": {
            "policy_version": rights_service.POLICY_VERSION,
            "all_commercial_use_allowed": all(a["licence"]["commercial_use_allowed"] for a in assets),
            "ai_generated_scenes": [a["scene_id"] for a in assets if a["is_ai_generated"]],
            "attribution_required_for": [a["scene_id"] for a in assets if a["attribution"]["required"]],
            "assets": assets,
        },
        "validation": {
            "outputs": validation,
            "scene_count": len(scenes),
            "repeat_clip_count": len(keys) - len(set(keys)),
            "coverage": {"first_start_frame": scenes[0]["start_frame"], "last_end_frame": scenes[-1]["end_frame"],
                         "total_frames": timeline["total_frames"],
                         "gapless": all(a["end_frame"] == b["start_frame"] for a, b in zip(scenes, scenes[1:]))},
            "min_scene_s": min(sc["duration_s"] for sc in scenes),
            "independent_judge_mean": (round(sum(sc["independent_judge"]["score_1_5"] for sc in scenes if sc.get("independent_judge")) /
                                       max(1, sum(1 for sc in scenes if sc.get("independent_judge"))), 3)
                                       if any(sc.get("independent_judge") for sc in scenes) else None),
        },
        "outputs": result,
        **(extra or {}),
    }


# ============================================================== swap (partial re-render)
def run_swap(job_id: str) -> dict:
    ctx = Ctx(job_id)
    s, req = ctx.s, ctx.req
    fps = s.output_fps
    parent_id = ctx.job.parent_job_id
    parent = repo.require_job(parent_id)
    pre = f"jobs/{parent_id}"
    with ctx.stage("loading_parent", 5):
        timeline = json.loads(ctx.storage.get_bytes(f"{pre}/timeline.json"))
        parent_manifest = json.loads(ctx.storage.get_bytes(f"{pre}/manifest.json"))
        candidates = json.loads(ctx.storage.get_bytes(f"{pre}/work/candidates.json")) if ctx.storage.exists(f"{pre}/work/candidates.json") else {}
        wav = ctx.storage.get_file(timeline["narration_object"], ctx.work / "narration.wav")
        transcript = Transcript.model_validate(json.loads(ctx.storage.get_bytes(timeline["transcript_object"])))
    sid = req["scene_id"]
    idx = next((i for i, sc in enumerate(timeline["scenes"]) if sc["scene_id"] == sid), None)
    if idx is None:
        raise AppError(ErrorCode.SCENE_NOT_FOUND, details={"scene_id": sid})
    scene = timeline["scenes"][idx]
    ratios = timeline["aspect_ratios"]
    pre_req = parent.request_json
    selector = FootageSelector(ctx.work, ratios, pre_req.get("style", ""), pre_req.get("avoid", []), s.seed + 7919,
                               Path(s.work_dir) / "_cache")
    selector.used_keys = {f"{sc['clip']['provider']}:{sc['clip']['asset_id']}" for sc in timeline["scenes"]}
    selector.used_hashes = [sc["qa"]["dhash"] for i, sc in enumerate(timeline["scenes"]) if i != idx and sc.get("qa", {}).get("dhash")]
    brief = SceneBrief(span_indices=[0], **{k: v for k, v in scene["visual_brief"].items() if k in SceneBrief.model_fields and k != "span_indices"})
    if req.get("query"):
        brief.primary_query = req["query"][:100]
        brief.alternative_queries = []
        brief.visual_concept = req["query"][:300]
    need = SceneNeed(sid, scene["text"], scene["duration_s"], brief)
    with ctx.stage("footage", 20):
        sel = None
        if req.get("asset_id"):
            prov = next((p for p in selector.stock if p.name == req.get("provider")), None)
            if prov is None:
                raise AppError(ErrorCode.INVALID_REQUEST, message=f"provider '{req.get('provider')}' is not enabled")
            try:
                cand = prov.get_asset(req["asset_id"])
            except ProviderError as exc:
                raise AppError(ErrorCode.FOOTAGE_PROVIDER_UNAVAILABLE, details={"reason": exc.reason}) from exc
            if cand is None:
                raise AppError(ErrorCode.NO_ELIGIBLE_FOOTAGE, details={"reason": "asset not found", "asset_id": req["asset_id"]})
            why = selector._hard_filters(cand, need)
            if f"{cand.provider}:{cand.asset_id}" in selector.used_keys:
                why.append("clip already used in this video")
            if why:
                raise AppError(ErrorCode.RIGHTS_CHECK_FAILED, details={"asset_id": cand.asset_id, "reasons": why})
            ranked = selector._score(need, [cand])
            got = selector._try_stock(need, ranked, [])
            if not got:
                raise AppError(ErrorCode.FOOTAGE_DOWNLOAD_FAILED, details={"asset_id": cand.asset_id})
            sc_, path, qa = got
            sel = _selection_from(sc_, path, qa, "user-selected replacement asset", need)
        elif req.get("candidate_rank"):
            from app.schemas.footage import ScoredCandidate
            pool = [ScoredCandidate.model_validate(x) for x in candidates.get(sid, [])]
            pool = [x for x in pool if x.candidate.key not in selector.used_keys]
            k = int(req["candidate_rank"]) - 1
            if k >= len(pool):
                raise AppError(ErrorCode.NO_ELIGIBLE_FOOTAGE, details={"reason": "no candidate at that rank", "available": len(pool)})
            got = selector._try_stock(need, [pool[k]], [])
            if not got:
                raise AppError(ErrorCode.FOOTAGE_DOWNLOAD_FAILED, details={"rank": req["candidate_rank"]})
            sc_, path, qa = got
            sel = _selection_from(sc_, path, qa, f"user picked stored candidate rank {req['candidate_rank']}", need)
        else:
            sel = selector.select(need)
    words = transcript.words
    cues = build_cues(words, s.caption_max_chars, s.caption_max_words)
    caps = timeline["captions"]
    t_render = time.perf_counter()
    new_segments, validation, result = {}, {}, {"videos": {}}
    with ctx.stage("partial_render", 40):
        for ratio in ratios:
            slug = ratio_slug(ratio)
            d = ctx.work / "segments" / slug
            d.mkdir(parents=True, exist_ok=True)
            out = d / f"{sid}.mp4"
            spec = spec_from_selection(sid, sel_dict(sel), scene["end_frame"] - scene["start_frame"], scene["start_s"], sel.clip_path)
            info = render_segment(spec, ratio, out, cues if caps.get("burn_in") else None, timeline.get("language"))
            scene.setdefault("render", {})[ratio] = info
            paths = []
            for sc in timeline["scenes"]:
                if sc["scene_id"] == sid:
                    paths.append(out)
                else:
                    paths.append(ctx.storage.get_file(sc["segments"][ratio], d / f"{sc['scene_id']}.mp4"))
            final = ctx.work / f"video_{slug}.mp4"
            concat_and_mux(paths, wav, timeline["total_frames"], final)
            W, H = canvas(ratio)
            validation[ratio] = validate_output(final, W, H, timeline["total_frames"])
            new_segments[ratio] = out
            ctx.progress("partial_render", 40 + 45 * (ratios.index(ratio) + 1) / len(ratios))
    rerender_s = round(time.perf_counter() - t_render, 3)
    failed = {r: v for r, v in validation.items() if not v["passed"]}
    if failed:
        raise AppError(ErrorCode.OUTPUT_VALIDATION_FAILED,
                       details={r: [k for k, ok in v["checks"].items() if not ok] for r, v in failed.items()})
    with ctx.stage("uploading", 90):
        old_clip = scene["clip"]
        for ratio in ratios:
            result["videos"][ratio] = ctx.put(f"video_{ratio_slug(ratio)}.mp4", ctx.work / f"video_{ratio_slug(ratio)}.mp4", "video/mp4")
            scene["segments"][ratio] = ctx.put(f"segments/{ratio_slug(ratio)}/{sid}.mp4", new_segments[ratio], "video/mp4")
        scene["clip"] = clip_record(sel, ctx.put(f"clips/{sid}.mp4", sel.clip_path, "video/mp4"))
        scene["selection"] = selection_record(sel, selector.weights, s.stock_min_score)
        scene["qa"] = {"source_luma": round(sel.luma, 3), "source_saturation": round(sel.saturation, 3), "dhash": sel.dhash,
                       "saliency_cx": round(sel.saliency_cx, 3), "saliency_spread": round(sel.saliency_spread, 3),
                       "black_free": True, "freeze_free": True}
        scene.pop("independent_judge", None)
        full_s = timeline.get("render_stats", {}).get("full_render_s")
        timeline["history"].append({
            "swap_job_id": job_id, "parent_job_id": parent_id, "scene_id": sid, "at": datetime.now(timezone.utc).isoformat(),
            "old_clip": {"provider": old_clip["provider"], "asset_id": old_clip["asset_id"]},
            "new_clip": {"provider": sel.candidate.provider, "asset_id": sel.candidate.asset_id},
            "reason": req.get("reason") or "user requested replacement",
            "rerender_s": rerender_s, "parent_full_render_s": full_s,
            "segments_rerendered": len(ratios), "segments_reused": (len(timeline["scenes"]) - 1) * len(ratios),
        })
        timeline["job_id"] = job_id
        timeline["parent_job_id"] = parent_id
        if ctx.storage.exists(f"{pre}/captions.srt"):
            result["captions_srt"] = ctx.storage.put_bytes(f"{ctx.prefix}/captions.srt", ctx.storage.get_bytes(f"{pre}/captions.srt"), "application/x-subrip")
        ctx.put_json("work/candidates.json", candidates)
        result["timeline_json"] = ctx.put_json("timeline.json", timeline)
        asr_meta = parent_manifest.get("providers", {}).get("asr", {})
        extra = {"partial_rerender": {"scene_id": sid, "rerender_s": rerender_s, "parent_full_render_s": full_s,
                                      "speedup_x": round(full_s / rerender_s, 2) if full_s and rerender_s else None,
                                      "segments_rerendered": len(ratios),
                                      "segments_reused": (len(timeline["scenes"]) - 1) * len(ratios)}}
        manifest = build_manifest(ctx, timeline, selector, asr_meta, parent_manifest.get("providers", {}).get("tts"),
                                  parent_manifest.get("input", {}), validation, result, timeline["duration_s"], rerender_s, extra)
        result["manifest_json"] = ctx.put_json("manifest.json", manifest)
    shutil.rmtree(ctx.work, ignore_errors=True)
    return result


def _selection_from(sc, path, qa, rule: str, need: SceneNeed) -> Selection:
    c = sc.candidate
    info = qa["info"]
    return Selection(candidate=c, clip_path=path, score=sc.score, components=sc.components, rank=sc.rank or 1,
                     rule=rule, reason=f"{c.provider} clip '{c.title[:70]}' chosen by user ({rule}); score {sc.score:.2f}",
                     in_point_s=qa["in_point"], clip_duration_s=info.duration_s, width=info.width, height=info.height,
                     saliency_cx=qa["cx"], saliency_spread=qa["spread"], luma=qa["luma"], saturation=qa["sat"],
                     dhash=qa["dhash"])
