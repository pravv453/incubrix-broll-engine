"""Stage 5-7: retrieve, gate, score, choose (stock vs generated), download, QA, dedup.

Selection rule between sources (Advanced level, measurable):
    best = highest-scoring rights-eligible, safe, unused stock candidate
    if best.score >= STOCK_MIN_SCORE (tau)            -> stock
    elif generation allowed for this scene             -> generated (AI) clip
    else (named person/brand, or generator down)       -> best stock with a low-relevance warning
Generation is never allowed for scenes naming real people or brands.
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.adapters.embeddings import get_embedder
from app.adapters.footage import get_generated_provider, get_stock_providers
from app.adapters.footage.base import ProviderError
from app.adapters.vision import get_vision
from app.adapters.vision.faces import get_face_detector
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.schemas.footage import FootageCandidate, ScoredCandidate
from app.schemas.scenes import SceneBrief
from app.services import content_policy as cp
from app.services import rights_service
from app.utils.ffmpeg import FFmpegError, detect_black, detect_freeze, dhash, extract_frame_jpg, hamming, luma_stats, probe, saliency_center_x

log = logging.getLogger("app.footage")
RATIO_AR = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0}


@dataclass
class SceneNeed:
    scene_id: str
    text: str
    duration_s: float
    brief: SceneBrief


@dataclass
class Selection:
    candidate: FootageCandidate
    clip_path: Path
    score: float
    components: dict
    rank: int
    rule: str
    reason: str
    in_point_s: float
    clip_duration_s: float
    width: int
    height: int
    saliency_cx: float
    saliency_spread: float
    luma: float
    saturation: float
    dhash: str | None
    alternatives: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    ranked: list[ScoredCandidate] = field(default_factory=list)


class FootageSelector:
    def __init__(self, work: Path, ratios: list[str], style: str, avoid: list[str], seed: int, cache_dir: Path):
        s = get_settings()
        self.s, self.work, self.ratios, self.style, self.seed = s, work, ratios, style, seed
        self.request_avoid = avoid
        self.avoid_terms = cp.expand_avoid(avoid)
        self.avoid_faces = cp.avoid_faces(avoid)
        self.stock, init_fail = get_stock_providers(cache_dir)
        self.generated = get_generated_provider()
        self.embedder = get_embedder()
        self.vision = get_vision()
        self.faces = get_face_detector()
        self.used_keys: set[str] = set()
        self.used_hashes: list[str] = []
        self.provider_failures: list[dict] = list(init_fail)
        self._failures_by_provider: dict[str, int] = {}
        self.counters = {"searches": 0, "generations": 0, "vision_calls": 0, "downloads": 0}
        (work / "clips").mkdir(parents=True, exist_ok=True)
        (work / "thumbs").mkdir(parents=True, exist_ok=True)
        weights = {"semantic_similarity": s.rank_w_semantic, "visual_concept_match": s.rank_w_visual,
                   "duration_fit": s.rank_w_duration, "aspect_fit": s.rank_w_aspect,
                   "quality_score": s.rank_w_quality, "rights_completeness": s.rank_w_rights}
        self.weights = weights

    # ------------------------------------------------------------ helpers
    def _circuit_open(self, name: str) -> bool:
        return self._failures_by_provider.get(name, 0) >= 2

    def _fail(self, name: str, scene_id: str, reason: str) -> None:
        self._failures_by_provider[name] = self._failures_by_provider.get(name, 0) + 1
        self.provider_failures.append({"provider": name, "scene_id": scene_id, "reason": reason})
        log.warning("provider_failed", extra={"provider": name, "scene_id": scene_id, "reason": reason})

    def _search(self, need: SceneNeed) -> list[FootageCandidate]:
        b = need.brief
        queries = list(dict.fromkeys([q for q in [b.primary_query, *b.alternative_queries] if q and q.strip()]))[:3]
        per = self.s.footage_max_candidates
        found: dict[str, FootageCandidate] = {}
        for prov in self.stock:
            if self._circuit_open(prov.name):
                continue
            for q in queries:
                if len(found) >= per * 2:
                    break
                try:
                    self.counters["searches"] += 1
                    for c in prov.search(q, per):
                        found.setdefault(c.key, c)
                except ProviderError as exc:
                    self._fail(prov.name, need.scene_id, exc.reason)
                    break
        return [c for k, c in found.items() if k not in self.used_keys]

    def _hard_filters(self, c: FootageCandidate, need: SceneNeed) -> list[str]:
        reasons = rights_service.check(c)
        hay = " ".join([c.title, " ".join(c.tags), c.description]).lower()
        hits = [t for t in self.avoid_terms if f" {t} " in f" {hay} " or f"{t}," in hay]
        if hits:
            reasons.append(f"matches avoid rule: {sorted(set(hits))[:4]}")
        if need.brief.sensitivity != "none":
            blocked = [t for t in cp.SENSITIVE_BLOCK if t in hay]
            if blocked:
                reasons.append(f"inappropriate for sensitive topic: {blocked[:3]}")
        if c.duration_s is not None and c.duration_s < 1.0:
            reasons.append("clip shorter than 1 s")
        return reasons

    def _aspect_fit(self, w: int | None, h: int | None) -> float:
        if not w or not h:
            return 0.5
        src = w / h
        fits = [min(src / RATIO_AR[r], RATIO_AR[r] / src) for r in self.ratios]
        return sum(fits) / len(fits)

    def _combine(self, comp: dict[str, float | None]) -> float:
        avail = {k: v for k, v in comp.items() if v is not None}
        wsum = sum(self.weights[k] for k in avail) or 1.0
        return round(sum(self.weights[k] * v for k, v in avail.items()) / wsum, 4)

    def _brief_text(self, need: SceneNeed) -> str:
        return f"{need.brief.visual_concept}. {need.text}"

    def _score(self, need: SceneNeed, cands: list[FootageCandidate]) -> list[ScoredCandidate]:
        if not cands:
            return []
        sims = self.embedder.similarities(self._brief_text(need), [c.text_for_embedding for c in cands])
        avoid_ctx = need.brief.must_avoid
        scored = []
        for c, sim in zip(cands, sims):
            hay = " ".join([c.title, *c.tags]).lower()
            if any(a.lower() in hay for a in avoid_ctx):
                sim *= 0.6  # literal misreading (e.g. "sky" for cloud computing)
            comp = {
                "semantic_similarity": round(sim, 4),
                "visual_concept_match": None,
                "duration_fit": round(min(1.0, (c.duration_s or need.duration_s) / max(need.duration_s, 0.1)), 4),
                "aspect_fit": round(self._aspect_fit(c.width, c.height), 4),
                "quality_score": round(min(1.0, min(c.width or 720, c.height or 720) / 1080), 4),
                "rights_completeness": round(rights_service.completeness(c), 4),
            }
            scored.append(ScoredCandidate(candidate=c, score=self._combine(comp), components=comp))
        scored.sort(key=lambda x: -x.score)
        # visual pass on the top few (thumbnail only: no video download)
        for sc in scored[:6]:
            c = sc.candidate
            thumb = None
            prov = next((p for p in self.stock if p.name == c.provider), None)
            if prov and (self.vision or (self.faces and self.avoid_faces)):
                try:
                    thumb = prov.thumbnail(c, self.work / "thumbs" / f"{need.scene_id}_{len(scored)}_{abs(hash(c.key)) % 10**8}.jpg")
                except Exception:  # noqa: BLE001
                    thumb = None
            if thumb and self.faces and self.avoid_faces and self.faces.count(thumb) > 0:
                sc.rejected = True
                sc.reject_reasons.append("face detected but request says avoid faces")
                continue
            if thumb and self.vision:
                self.counters["vision_calls"] += 1
                sc.components["visual_concept_match"] = round(self.vision.score(need.brief.visual_concept, [thumb]) or 0, 4)
                sc.score = self._combine(sc.components)
        ok = sorted([x for x in scored if not x.rejected], key=lambda x: -x.score)
        for i, x in enumerate(ok):
            x.rank = i + 1
        return ok + [x for x in scored if x.rejected]

    def _probe_clip(self, need: SceneNeed, c: FootageCandidate, path: Path) -> tuple[dict | None, str | None]:
        """Decode check, window choice, black/freeze QA, perceptual dedup."""
        try:
            info = probe(path)
        except FFmpegError:
            return None, "download is corrupt / not decodable"
        if not info.has_video or not info.width or info.duration_s <= 0.5:
            return None, "no usable video stream"
        need_s = need.duration_s
        slack = max(0.0, info.duration_s - need_s)
        in_point = round(min(slack, max(0.0, slack * 0.15)), 3)
        window = min(need_s, info.duration_s - in_point)
        try:
            if detect_black(path, 0.4, in_point, window):
                return None, "black frames in the needed window"
            if detect_freeze(path, 1.0, -60, in_point, window, pre_vf="crop=iw:ih*0.72:0:0"):
                return None, "frozen frames in the needed window"
            h = dhash(path, in_point + window / 2)
        except FFmpegError:
            return None, "decode failed during QA"
        if h and any(hamming(h, u) <= 5 for u in self.used_hashes):
            return None, "visually duplicates a clip already used in this video"
        cx, spread = saliency_center_x(path, in_point, window)
        luma, sat = luma_stats(path, in_point, min(window, 4))
        return {"info": info, "in_point": in_point, "dhash": h, "cx": cx, "spread": spread, "luma": luma, "sat": sat}, None

    def _try_stock(self, need: SceneNeed, ranked: list[ScoredCandidate], rejected: list[dict]) -> tuple[ScoredCandidate, Path, dict] | None:
        for sc in [x for x in ranked if not x.rejected][: self.s.candidates_to_probe]:
            c = sc.candidate
            prov = next((p for p in self.stock if p.name == c.provider), None)
            if prov is None:
                continue
            dest = self.work / "clips" / f"{need.scene_id}_{c.provider}_{c.asset_id}.mp4"
            try:
                self.counters["downloads"] += 1
                prov.download(c, dest)
            except ProviderError as exc:
                rejected.append({"provider": c.provider, "asset_id": c.asset_id, "reasons": [exc.reason]})
                continue
            qa, why = self._probe_clip(need, c, dest)
            if why:
                rejected.append({"provider": c.provider, "asset_id": c.asset_id, "reasons": [why]})
                continue
            return sc, dest, qa
        return None

    def _try_generated(self, need: SceneNeed, primary_ratio: str) -> tuple[ScoredCandidate, Path, dict] | None:
        if not self.generated or self._circuit_open("generated"):
            return None
        concept = need.brief.visual_concept
        try:
            self.counters["generations"] += 1
            cand, clip, img = self.generated.generate(
                need.scene_id, concept, self.style, self.request_avoid + need.brief.must_avoid, primary_ratio,
                need.duration_s, self.work / "clips", self.seed + len(self.used_keys))
        except ProviderError as exc:
            self._fail("generated", need.scene_id, exc.reason)
            return None
        if self.faces and self.faces.count(img) > 0:
            self._fail("generated", need.scene_id, "generated image contains a face; discarded by safety policy")
            return None
        sim = self.embedder.similarities(self._brief_text(need), [cand.description])[0]
        comp = {"semantic_similarity": round(max(sim, 0.0), 4), "visual_concept_match": None, "duration_fit": 1.0,
                "aspect_fit": round(self._aspect_fit(cand.width, cand.height), 4), "quality_score": 1.0,
                "rights_completeness": round(rights_service.completeness(cand), 4)}
        if self.vision:
            self.counters["vision_calls"] += 1
            comp["visual_concept_match"] = round(self.vision.score(concept, [img]) or 0, 4)
        sc = ScoredCandidate(candidate=cand, score=self._combine(comp), components=comp, rank=1)
        qa, why = self._probe_clip(need, cand, clip)
        if why:
            self._fail("generated", need.scene_id, why)
            return None
        return sc, clip, qa

    # ------------------------------------------------------------ main
    def select(self, need: SceneNeed) -> Selection:
        rejected: list[dict] = []
        warnings: list[dict] = []
        cands = self._search(need)
        eligible = []
        for c in cands:
            why = self._hard_filters(c, need)
            if why:
                rejected.append({"provider": c.provider, "asset_id": c.asset_id, "reasons": why})
            else:
                eligible.append(c)
        ranked = self._score(need, eligible)
        for x in ranked:
            if x.rejected:
                rejected.append({"provider": x.candidate.provider, "asset_id": x.candidate.asset_id, "reasons": x.reject_reasons})
        live = [x for x in ranked if not x.rejected]
        best = live[0].score if live else None
        tau = self.s.stock_min_score
        gen_allowed = self.generated is not None and not need.brief.named_entities
        primary_ratio = self.ratios[0]
        chosen = None
        rule = ""
        if best is not None and best >= tau:
            chosen = self._try_stock(need, ranked, rejected)
            rule = f"stock: best eligible stock score {best:.2f} >= tau {tau:.2f}"
        if chosen is None and gen_allowed:
            chosen = self._try_generated(need, primary_ratio)
            if chosen:
                rule = (f"generated: best stock score {best:.2f} < tau {tau:.2f}" if best is not None and best < tau
                        else "generated: no usable rights-eligible stock clip" if best is None
                        else "generated: stock candidates failed download/QA")
        if chosen is None and live:
            chosen = self._try_stock(need, ranked, rejected)
            if chosen:
                why = ("scene names a real person/brand, so AI generation is not allowed"
                       if need.brief.named_entities else "generated source unavailable")
                rule = f"stock (below tau): {why}; best stock score {best:.2f}"
                if best is not None and best < tau:
                    warnings.append({"code": "LOW_RELEVANCE_STOCK", "scene_id": need.scene_id,
                                     "message": f"best stock score {best:.2f} < tau {tau:.2f}; {why}"})
        if chosen is None:
            down = {f["provider"] for f in self.provider_failures}
            all_down = self.stock and all(p.name in down for p in self.stock) and (not gen_allowed or "generated" in down)
            raise AppError(
                ErrorCode.FOOTAGE_PROVIDER_UNAVAILABLE if all_down else ErrorCode.NO_ELIGIBLE_FOOTAGE,
                details={"scene_id": need.scene_id, "query": need.brief.primary_query,
                         "rejected": rejected[:10], "provider_failures": self.provider_failures[-5:]})
        sc, path, qa = chosen
        c = sc.candidate
        self.used_keys.add(c.key)
        if qa["dhash"]:
            self.used_hashes.append(qa["dhash"])
        info = qa["info"]
        comp_txt = ", ".join(f"{k.split('_')[0]} {v:.2f}" for k, v in sc.components.items() if v is not None)
        reason = (f"{'AI-generated clip' if c.is_ai_generated else c.provider + ' clip'} '{c.title[:70]}' matches the "
                  f"brief '{need.brief.visual_concept[:90]}' (score {sc.score:.2f}: {comp_txt}). Rule: {rule}.")
        alts = [{"provider": x.candidate.provider, "asset_id": x.candidate.asset_id, "title": x.candidate.title[:80],
                 "score": x.score} for x in live if x.candidate.key != c.key][:3]
        return Selection(
            candidate=c, clip_path=path, score=sc.score, components=sc.components, rank=sc.rank or 1, rule=rule,
            reason=reason, in_point_s=qa["in_point"], clip_duration_s=info.duration_s, width=info.width,
            height=info.height, saliency_cx=qa["cx"], saliency_spread=qa["spread"], luma=qa["luma"],
            saturation=qa["sat"], dhash=qa["dhash"], alternatives=alts, rejected=rejected[:12], warnings=warnings,
            ranked=live[:10],
        )

    def frames_for_judge(self, sel: Selection, need_s: float, scene_id: str) -> list[Path]:
        out = []
        for k, f in enumerate((0.2, 0.5, 0.8)):
            t = sel.in_point_s + min(need_s, sel.clip_duration_s - sel.in_point_s) * f
            out.append(extract_frame_jpg(sel.clip_path, t, self.work / "thumbs" / f"{scene_id}_judge{k}.jpg"))
        return out
