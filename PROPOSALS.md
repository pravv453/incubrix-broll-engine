# Proposed changes to the plan (for your review)

**Decision (2026-11-22):** all accepted except **#2 (approved fallback asset pack)**, which was rejected for now
("no fallback mechanism"). Provider failover between the two real sources (stock ↔ generated) stays,
because must-have #7 requires it. See IMPLEMENTATION.md for where each item lives.
Items marked "verify" need a licence or terms check before shipping. I have not checked them online.

## A. Changes that affect must-haves or pass/fail risk

1. **Second footage source = generated clips, not Pexels/Openverse.**
   The brief's own example is "a stock library and a generative video or image-to-video model".
   Proposal: Pixabay (stock) + an AI image model rendered as a slow pan/zoom ("image-to-video"),
   flagged `is_ai_generated: true`. Measurable rule: *use stock if best eligible stock score ≥ τ (e.g. 0.55),
   else generate*. It also covers abstract lines ("trust is earned slowly") and the case where a provider is down.
   The generator is never used for scenes that mention a named person or brand.
   Pexels: new keys are reportedly paused. Openverse/Flickr: licence metadata is aggregator-reported and watermarks are common. Both are too risky for must-have #3.
   Cost is per generated image and has to be measured; it is cheaper than text-to-video models.

2. **Fallback must be an approved asset pack, not the mock provider.**
   Mock clips are synthetic test patterns. Used in the live demo they would score 1/5 on relevance.
   Proposal: a small, owned/CC0 "neutral B-roll" pack (abstract motion, nature, city, office) with recorded
   licence evidence. It is used only when every provider fails, and a warning is recorded.

3. **Mock and fallback clips must contain motion.**
   A static colour clip or a still image will trip our own frozen-frame check (must-have #4).
   Mock fixtures will use moving sources (ffmpeg `testsrc2`/gradients). Stills always get Ken Burns motion.

4. **Per-clip QA before selection, not only after the final render.**
   Run `freezedetect`/`blackdetect` on each downloaded candidate's trimmed window and reject failures
   (e.g. a near-static stock timelapse). Then move to the next ranked candidate. Post-render QA stays as the final gate.

5. **Enforce `avoid: ["people's faces"]` visually, not just with query words.**
   Stock tags miss faces. Run a face detector on 3 sampled frames per candidate (OpenCV YuNet, verify licence)
   and reject on detection. The same detector drives the named-person safety rule.

6. **Smart crop for 9:16 (hard case: subject at the edge).**
   Pick the crop window from a saliency/face centroid on sampled frames instead of a centre crop. If the subject
   spread is too wide for the crop, fall back to a blurred-background pad. Record the policy per clip.
   **Do not use Ultralytics YOLO** (AGPL, which the brief forbids in the shipped service).

7. **Rule-based scene planner as the LLM fallback.**
   Instead of a local Qwen/Llama adapter (heavy, licence work, slow on CPU): a deterministic planner
   (sentence groups + noun-phrase queries + style). It is the mock in tests *and* the fallback when Gemini fails,
   so a planner outage never fails a job. The LLM only groups sentence indices and writes visual briefs.
   Python owns the boundaries (already implemented: cuts at the midpoint of the pause between words).

8. **Add error codes `NO_SPEECH_DETECTED` (silent/music-only) and `MISSING_IDEMPOTENCY_KEY`.**
   These are not in idea.md's list but the hard cases need them. Detection: Silero VAD speech ratio (MIT, verify)
   plus ASR no-speech probability. *(Already added to the code catalogue.)*

## B. Changes that raise relevance (Core 3.5 / Outstanding 85% ≥ 4)

9. **Score thumbnails visually in the default ranking (SigLIP, verify licence; base checkpoint is fine on CPU).**
   Stock tags are weak, which is exactly the "value → cash" problem. Provider thumbnails need no video download.
   This fills the 0.20 "visual" weight from day one.

10. **Keep the Outstanding "independent" scorer separate from the selector.**
    If SigLIP picks the clips, SigLIP cannot also be the independent judge. Use Gemini multimodal as the judge
    on sampled frames, and report Spearman ρ / weighted κ against human ratings via an `eval/` script.

11. **Disambiguation via context briefs + negative queries.**
    The planner emits `must_avoid` per scene (e.g. "cloud computing" → avoid "sky, weather").
    Candidates whose tags or visual score match the negatives are penalised. Unit tests cover value/bank/cloud/mouse/python.

12. **For script input, align ASR words back to the original script text** (difflib).
    Timing comes from ASR of the real TTS audio; spelling, numbers and punctuation come from the script,
    so captions show "40%" rather than "forty percent".

## C. Advanced and Outstanding mechanics

13. **Segment-based rendering for partial re-render.** Render each scene to its own normalised segment
    (exact frame count, identical encode settings, captions burned per segment with time offset).
    Then use a stream-copy concat and mux the narration once. A clip swap re-encodes one segment and
    re-runs the copy-concat, which takes seconds. Timings for swap vs full render are recorded in the manifest as evidence.

14. **Colour/brightness normalisation:** measure per-clip mean luma/saturation (`signalstats`) and apply an
    `eq` correction toward a target, which makes "looks like one video" measurable.

15. **Hindi/Tamil:** translate visual briefs to English queries (stock metadata is English), multilingual-e5
    for embeddings, Gemini for ASR where Whisper-small is weak, and the danda (।) as a sentence boundary
    *(already handled)*. Noto fonts are in the image *(done)*. Real Hindi/Tamil voiceovers must be your own recordings
    or ones you have consent for.

## D. Operations and evidence

16. **Live demo hosting:** one VM running the same compose file behind Caddy (TLS + bearer token + rate limit).
    Free PaaS tiers can't run 5 containers + ffmpeg. Size the VM at about 4 vCPU / 8–16 GB and measure concurrency on it.

17. **Batch runner writes an evidence index + a ratings CSV template** (scene id, timestamp range, clip link,
    blank 1–5 column). Human rating and the coverage/relevance metrics are then computed by script, not by hand.

18. **Pin versions via `uv.lock`** (already in place) and record model revisions in SOURCES.md and the manifest.

19. **Keep the brief PDF out of the repo.** It is marked confidential. Only idea.md, pipeline.md and prompt.md are copied in.

## E. Things in the docs I would change

- `pipeline.md` sets `GEMINI_TEXT_MODEL=gemini-3.8-flash`. I can't confirm that ID exists. The default is left empty
  and must be set from the live model catalogue.
- `FOOTAGE_PROVIDERS=pixabay,mock` should become `pixabay,generated` with the approved pack as the last-resort fallback (see #2).
- Drop the `pexels.py` and `local_transformers.py` adapters unless you already hold a Pexels key / want local LLMs.
