# Report: Script & Voiceover → B-roll Engine

## 1. Problem and approach

The service takes a voiceover, a script or an IncuBrix scene list and returns finished B-roll videos: relevant
footage for every line, timed to the narration, with captions, and a rights record for every clip.

**Approaches considered**

| Option | Why not (or why) |
|---|---|
| LLM writes the whole edit (scenes + timestamps + clip choice) | Timestamps would be hallucinated and rights decisions would be un-auditable. Rejected |
| Keyword search per sentence | The brief's own failure mode ("value" → cash, "cloud computing" → sky). Rejected as the sole signal |
| Text-to-video generation for every scene | Slow, expensive, inconsistent, and risky for real people and brands. Used only as the second source below a measurable threshold |
| **Chosen: deterministic pipeline + models as advisers** | ASR is the only source of timing. The planner only groups sentence indices and writes visual briefs. A rule-based safety layer, a hard rights gate, ranking, QA and rendering are ordinary tested code |

## 2. Design highlights

1. **Timing is computed, never guessed.** Scene cuts sit at the midpoint of the pause between the last word of
   one scene and the first word of the next. Boundaries are frame-exact from absolute time, scene 1 starts at
   frame 0, and the last scene ends at `round(narration_s × 30)`. A validator fails the job on any gap or overlap.
   For text input, the script's own words are aligned onto the ASR timings, so captions show "40%" rather than
   "forty percent" while timing comes from the real audio.
2. **Meaning over keywords.** Visual briefs come from the planner (Gemini structured output, or the rules planner
   with a context-disambiguation table). A deterministic safety layer then always runs: named people and brands are
   stripped from queries and AI generation is forbidden for them; sad news gets calm visuals and a block-list;
   statistics and abstract lines are marked *illustrative*. Ranking combines text embeddings (brief vs clip
   metadata) with CLIP on thumbnails, and penalises literal misreadings (`must_avoid`, e.g. "sky").
3. **Rights are a hard gate.** Allow-listed provider, licence name and URL, commercial use, accepted evidence
   status, no watermark, no editorial-only restriction, attribution available if required. Relevance can never
   buy back a failed rights check. Every clip's record goes into `timeline.json` and `manifest.json`.
4. **Two sources with a measurable rule.** Stock is used if the best eligible stock score ≥ τ = 0.55. Otherwise
   an AI-generated clip (image → slow pan/zoom) is used, unless the scene names a real person or brand, in which
   case the best stock clip is used with a warning. The rule that fired is stored per scene.
5. **Correct video by construction, then verified.** One segment per scene and ratio with an exact frame count
   and identical encoder settings, then a stream-copy concat and a single narration mux. QA checks codec, pixel
   format, dimensions, fps, frame count, A/V drift ≤ 1 frame, black frames and frozen frames, both on each
   candidate clip and on the final file. A crop that isolates a static region is self-healed by re-rendering
   with a full-frame blur-pad.
6. **Partial re-render.** A swap re-encodes only the changed scene's segment per ratio and reuses every other
   segment by storage key.

## 3. What works (evidence in `test-results/` and the test suite)

| Area | Status |
|---|---|
| Must-haves 1–8 | Implemented. Automated evidence in `tests/` (see IMPLEMENTATION.md §12) |
| Integration Standard 1–10 | Implemented. `docker compose up`, `docker compose run --rm tests`, `openapi.yaml` parity test, idempotency incl. a concurrency test, error catalogue in the README, env-only config, upload + URL inputs, S3 outputs with presigned links, JSON logs + `/health` + `/ready`, manifest, batch runner |
| Advanced | Two sources + rule + reason ✅; swap with partial re-render ✅; colour normalisation + min 1.5 s scenes ✅; scene lists ✅ |
| Outstanding | Judge + agreement script ✅ (needs a key and human ratings); Hindi/Tamil plumbing ✅ (needs real voiceovers + Gemini to verify quality); production numbers: partly measured (below) |

## 4. What does not work yet / limitations (honest list)

- **Real-provider runs not yet done in this log.** Gemini (TTS, planner, image, judge) and Pixabay adapters are
  implemented against the official APIs but have not been exercised with keys here. Every keyless test uses mocks.
- **Relevance numbers need human ratings.** No Core relevance average, % ≥ 4 or judge-human agreement is claimed
  until `ratings_template.csv` is filled and `eval_relevance.py` is run on real-provider outputs.
- **Accents and Hindi/Tamil** need real recordings (own or consented). The rules planner cannot translate
  non-English narration into English stock queries; that needs `LLM_PROVIDER=gemini`.
- **Music with vocals** may pass the VAD check as "speech". Pure instrumental music is rejected.
- **Saliency crop** is a motion + detail heuristic, not subject detection. Wide-interest shots fall back to blur-pad.
- **Mock TTS** is a placeholder tone (jobs say so with a `MOCK_NARRATION` warning). Real narration needs Gemini TTS.
- **Rate limiter** is per API process (fine for the single-replica demo).

## 5. Measured numbers

Hardware: Apple M4 (10-core) laptop, 16 GB; Docker in a Colima VM with 4 vCPU / 8 GB. CPU only, no GPU.
Configuration unless noted: faster-whisper `small` int8, bge-small, CLIP B/32, YuNet; mock TTS, rules planner,
mock stock and mock generator (no paid APIs).

| Metric | Value | Conditions |
|---|---|---|
| Turnaround, 14.2 s real voiceover (MP3 upload), 2 ratios | 202 s cold (incl. first model downloads; transcription stage 75.7 s) | 1 run |
| Same input after a worker restart (models cached on disk) | 87 s | 1 run |
| Turnaround for 60 s of narration, cold/warm | TBD | to be measured with `06_saving_money_60s` (TESTING.md §10) |
| Cost per finished minute | $0 with keyless defaults (local models, mock footage) + compute. Gemini/Pixabay: TBD | unit prices go in `COST_*`; the manifest reports per-minute cost and lists unknown items |
| Concurrency | TBD | `--concurrency 4` with `MAX_CONCURRENT_JOBS=2` |
| A/V drift | 0.3 ms | 14.2 s real voiceover, 16:9 |
| Repeat-clip rate | 0 in every test and run so far | enforced by asset key + perceptual hash |
| Render success rate | TBD across the full scenario pack | |
| Peak memory | TBD | `--sample-resources` |

## 6. Cost model

Stock (Pixabay) is free. Local ASR, embeddings, CLIP and face detection cost only compute. Paid items are
Gemini TTS (per character), the planner (per call), image generation (per generated scene) and the judge (per scene).
The manifest multiplies usage counters by the `COST_*` unit prices. Blank prices are reported as unknown, never guessed.

## 7. Next steps

1. Run the core, hard and multilingual packs with Gemini and Pixabay keys, rate every scene, and publish relevance and agreement numbers.
2. Measure 60 s turnaround (cold and warm) and concurrency on the demo server. Deploy behind TLS with a token and rate limit.
3. Subject-aware crop (face/object boxes from the existing detector) and shot-boundary-aware in-points.
4. Cache embeddings and CLIP scores per provider asset across jobs; batch thumbnail scoring.
5. Per-language planner evaluation for Hindi/Tamil with native-speaker ratings.
