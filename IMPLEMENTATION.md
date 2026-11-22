# Implementation log

A running record of every feature and step: what was built, where it lives, how it is verified, and what went wrong on the way.
Newest entries are at the bottom of each section. Status: ✅ done and tested · 🟡 implemented, needs real-key or manual verification · ⬜ not done.

---

## 0. Environment setup (2026-11-22)

| Step | Detail |
|---|---|
| Python | `uv` installed in user space; Python 3.12 via `uv python install 3.12` (system Python was 3.9) |
| FFmpeg | Static macOS arm64 ffmpeg/ffprobe 9.1 (martin-riedl.de) in `~/.local/bin`, used for local tests. The image uses Debian's ffmpeg |
| Docker | No admin rights, so no Docker Desktop. Installed Colima 0.10.3 + Lima 2.3.0 (Apple Virtualization, `--vm-type vz`, 4 CPU / 8 GB), Docker CLI 29.10, compose 5.8, buildx 0.39, all in user space |
| Repo | `~/incubrix-broll-engine`, git initialised. The brief PDF is kept out of the repo (marked confidential) |

## 1. Foundations (Phase A) ✅

| Feature | Files | Verified by |
|---|---|---|
| Settings from env only; `.env.example` lists every setting (parity enforced) | `app/core/config.py`, `.env.example` | `tests/unit/test_core.py::test_env_example_lists_every_setting` |
| Standard error envelope + code catalogue (single source of truth, README table generated from it) | `app/core/errors.py`, `scripts/gen_error_table.py` | `test_core.py`, `test_api.py::test_readme_lists_every_error_code` |
| JSON logs with `timestamp, level, job_id, correlation_id`; secrets redacted | `app/core/logging.py` | `test_core.py::test_json_log_has_required_fields` |
| `/health`, `/ready` (DB, Redis, MinIO probes; 503 + details) | `app/api/routes/health.py` | `test_core.py::test_ready_*` |
| Request schema: exactly one input, empty script, aspect ratio, unknown fields rejected | `app/schemas/requests.py` | `test_core.py`, `test_api.py::test_validation_errors` |

## 2. Deterministic timing core ✅

| Feature | Detail | Files |
|---|---|---|
| Sentence/phrase spans | Split at `. ! ? … । ॥`. Sentences longer than `MAX_SCENE_S` are split at the comma nearest the middle, else at the longest pause (ties → nearest the middle) | `app/services/segmentation.py` |
| Gap-free frame-exact scenes | Each cut is placed at the **midpoint of the pause** between the last word of one scene and the first word of the next. Scene 1 starts at frame 0 and the last ends at `round(narration_s × 30)`. Every boundary is computed from absolute time, so there is no drift | same |
| No flashing clips | Scenes shorter than `MIN_SCENE_S` (1.5 s) merge into the shorter neighbour | same |
| Coverage validator | Hard invariants: start at 0, end at the total, contiguous, every word assigned once. A violation fails the job | same |
| Captions | Word-timed SRT cues (≤ 42 chars / 8 words, break on pauses > 0.6 s and sentence ends, never overlapping) | `app/services/caption_service.py` |

**Bug found by tests:** an unpunctuated long sentence split at its first word when pauses were equal. Fixed with a midpoint tie-break.

## 3. Jobs, idempotency, storage (Phase B) ✅

| Feature | Detail | Files |
|---|---|---|
| Durable jobs | `jobs` table: status, stage, progress, warnings, result keys, error, attempts, cancel flag, timestamps | `app/db/models.py` |
| Idempotency | Unique constraint on `idempotency_key` plus a canonical SHA-256 of the request (an uploaded file is hashed by content). Same key + same body → same job, enqueued once. Different body → `409 IDEMPOTENCY_CONFLICT`. Safe under concurrent inserts (the loser of the race reads the winner's row) | `app/db/repositories.py` |
| Monotonic progress | `update_job` clamps progress to `max(old, new)` | same |
| Storage | S3/MinIO adapter with presigned links signed for the public host; a local backend for the no-dependency test path (atomic `.part` → rename) | `app/storage/s3_storage.py` |
| Workers | Celery (acks_late, reject on worker loss, prefetch 1, concurrency = `MAX_CONCURRENT_JOBS`); inline thread runner for tests. Same `execute()` for both | `app/workers/` |
| Callback | `POST` on the terminal state, `X-IncuBrix-Signature` = hex HMAC-SHA256 of the raw body, SSRF-checked, optional host allowlist, 3 retries | `app/workers/runner.py` |

## 4. Inputs, narration, ASR (Phase C) ✅ / 🟡

| Feature | Detail |
|---|---|
| Upload validation (inline, < 1 s) | ffprobe of the **actual stream**: MP3/WAV/M4A container plus an audio codec allow-list. Rejects wrong types, videos renamed to `.mp3`, > 10 min (`AUDIO_TOO_LONG`), > `MAX_UPLOAD_MB` |
| SSRF-safe URL fetch | http(s) only, no credentials in the URL. Every resolved IP must be global (blocks private, loopback, link-local, metadata, IPv4-mapped IPv6). Redirects followed manually with a re-check per hop. Size and time limits |
| Canonical narration | Everything is converted to 48 kHz mono PCM, used for ASR, VAD and the final mux |
| No-speech detection | `volumedetect` (silent → `NO_SPEECH_DETECTED`) plus Silero VAD speech ratio < `MIN_SPEECH_RATIO` (music-only). A transcript with < 2 words or low confidence also fails clearly |
| ASR | faster-whisper with `word_timestamps=True`, VAD filter, hallucination guard (`no_speech_prob`), monotonic clamping 🟡 (real model exercised in Docker runs) |
| TTS | Gemini TTS adapter (chunks at sentence boundaries, PCM → WAV, retries) 🟡 needs a key; mock TTS produces a speech-like tone whose length matches the mock ASR's speaking rate |
| Script alignment (improvement) | For text input, the script's own words (spelling, "40%", punctuation) are aligned onto the ASR timings with difflib. Unmatched runs are interpolated. Captions therefore show the script, while timing comes from the real audio. The alignment ratio is recorded in the manifest |

## 5. Scene planning + safety layer (Phase D) ✅ / 🟡

| Feature | Detail |
|---|---|
| Planner contract | The planner returns **span-index groups + visual briefs only**. It can never return timestamps. `validate_grouping` rejects anything that is not an ordered, gap-free partition |
| Rules planner (default, mock) | One scene per sentence unless it is under half the target length. Context disambiguation table (cloud/bank/mouse/python/value/apple/java/crane/spring). Abstract-concept metaphors. Neutral data visuals for statistics. Named-entity stripping. Sensitive-topic visuals |
| Gemini planner 🟡 | `google-genai` structured output (`response_schema=ScenePlan`), seeded, temperature 0.2, one repair retry with the validation error, then `SCENE_PLANNING_FAILED`. Fixed-grouping mode for IncuBrix scene lists |
| Deterministic safety layer | Runs after **any** planner and unions its flags with the planner's: sensitivity (tragic/sensitive lexicons, incl. Hindi/Tamil words), named people/brands (stripped from queries; generation forbidden), statistics |
| IncuBrix scene lists | Each scene's text becomes one fixed scene. `visual_hint` becomes the primary query and brief. Scene times come from aligning the narration to the scene texts |

**Bug found by tests:** `SceneBrief.must_avoid` was capped at 10 items, but the sensitive-topic block list has 19. Every sad-news script would have crashed. The cap was raised to 40.

## 6. Footage: rights, ranking, two sources, dedup (Phase E) ✅ / 🟡

| Feature | Detail | Files |
|---|---|---|
| Provider interface | `search`, `download`, `thumbnail`, `get_asset`. Runtime fault injection (`down / empty / corrupt / slow`) read from `FAULT_INJECTION_FILE` on every call | `app/adapters/footage/base.py` |
| Mock stock library | 44 assets with realistic metadata, rendered on demand as **moving** synthetic clips. It deliberately includes watermarked, unlicensed, editorial-only, corrupt, too-short, face and alcohol assets so every gate is exercised | `mock.py` |
| Pixabay adapter 🟡 | Retries with backoff and `Retry-After`, 24 h response cache, download-host allow-list, size limit, safesearch. Records licence/creator/page per clip | `pixabay.py` |
| Generated source | AI image → Ken Burns motion clip (upscaled zoompan, 4 motion variants). Safety suffix in the prompt (no faces/text/logos), face check on the image, `is_ai_generated: true`, never for named entities. Gemini backend 🟡, mock backend ✅ | `generated.py` |
| Rights gate (hard) | Allow-listed provider, asset id + source link, licence name + URL, commercial use, accepted evidence status, not watermarked, not editorial-only, attribution available if required, generated ⇒ flagged | `app/services/rights_service.py` |
| Ranking | Configurable weights (0.45/0.20/0.10/0.10/0.10/0.05), renormalised over available components. Literal misreadings (`must_avoid`, e.g. "sky" for cloud computing) are penalised. CLIP score on thumbnails for the top 6 | `app/services/footage_service.py` |
| Selection rule (Advanced) | Stock if best ≥ τ (0.55). Else generated (if allowed). Else best stock with a `LOW_RELEVANCE_STOCK` warning. The rule text is stored per scene | same |
| Clip QA before acceptance | Decode check, window choice, black and freeze detection on the exact window (same caption-band crop as the final QA), dHash perceptual dedup (Hamming ≤ 5) | same |
| Circuit breaker | A provider is skipped for the rest of the job after 2 failures. Failures are recorded per scene | same |
| Avoid rules | Free text is expanded to match terms (e.g. "people's faces" → face/portrait/selfie…, "alcohol" → beer/wine/bar…) and hard-filtered on metadata. Faces are also checked visually with YuNet when the request avoids faces | `content_policy.py`, `vision/faces.py` |

## 7. Rendering + QA (Phase F) ✅

| Feature | Detail |
|---|---|
| Per-scene segments | One segment per scene × ratio with **exact frame count**, `fps=30`, `yuv420p`, libx264 (identical settings, `-video_track_timescale 15360`), no source audio |
| Smart crop (hard case: subject at the edge) | Motion + detail saliency centroid → horizontal crop window. If interest is spread too wide for a 9:16 crop → blurred-background pad. The policy is recorded per scene and ratio |
| Colour normalisation | Per-clip mean luma/saturation (`signalstats`) → bounded `eq` correction toward `TARGET_LUMA` |
| Short clips | Gentle slow-motion up to 1.5×. Beyond that, a loop with a `CLIP_LOOPED` warning |
| Captions | ASS generated per segment with a time offset (PlayRes = output size, Noto fonts, Devanagari/Tamil font by language), burned with libass |
| Concat + mux | Stream-copy concat, narration muxed once (AAC 192k), `apad` + `-t` = exact frame duration, `+faststart` |
| Segment self-healing (improvement) | After each segment render: freeze check. If a crop isolated a static region → re-render with the full-frame blur-pad. If still frozen → `OUTPUT_VALIDATION_FAILED` |
| Output QA | h264, aac, yuv420p, exact WxH, 30 fps, frame count ±1, audio present, A/V drift ≤ 1 frame + 1 AAC frame, no black (≥ 0.25 s), no frozen (≥ 1 s, caption band excluded). A failed render is kept under `failed/` and the job fails |

**Bug found by tests:** the scene-list job failed final QA with a 1.2 s freeze in 9:16. The saliency crop had landed on a flat region of a slowly zooming clip, and the final QA (downscaled) was stricter than the per-clip QA. Fixes: identical QA definitions at both stages, segment-level self-healing, and continuous motion in mock clips.

## 8. Timeline, manifest, observability (Phase G) ✅

| Output | Contents |
|---|---|
| `timeline.json` | Per scene: text, start/end seconds and frames, word range, visual brief (+ illustrative flag), clip (provider, asset, source URL, licence, attribution, AI flag, generation prompt, in-point), selection (score, components, weights, rank, rule, τ, reason, alternatives, rejected candidates with reasons), QA (luma, dHash, saliency), render info per ratio (crop policy, colour correction, speed, loop), segment keys, warnings, independent judge. Top level: fps, total frames, narration/transcript objects, render stats, swap history |
| `manifest.json` | App version + git SHA; providers + models (TTS, ASR, planner, embeddings, vision, faces, judge, stock, generated, ffmpeg); library versions; settings snapshot (secrets masked); ranking weights and rule; seed; per-stage timings + turnaround; usage counters; cost estimate (unit prices from `COST_*`, unknown items listed, per finished minute); warnings; provider failures; rights (policy version, per-asset records, all-commercial flag, AI scenes); validation (per-output checks, repeat-clip count, coverage, min scene, judge mean) |
| Job status | `queued → running → succeeded/failed/cancelled`, stage, monotonic progress, warnings, standard error, presigned result links, timeline URL |

## 9. Advanced (Phase H) ✅ / 🟡

| Item | Status |
|---|---|
| Two sources + measurable rule + reason in the timeline | ✅ (mock stock + mock generated). 🟡 with Pixabay + Gemini image |
| Clip swap with partial re-render | ✅ `POST /jobs/{id}/scenes/{scene_id}/clip`: next-best, `candidate_rank`, `query`, or `provider+asset_id` (rights-gated, no reuse). Re-encodes one segment per ratio and reuses all others by storage key. The manifest records `rerender_s` vs `parent_full_render_s` |
| Colour/brightness normalisation + pacing (≥ 1.5 s scenes) | ✅ |
| IncuBrix scene lists | ✅ |

## 10. Outstanding (Phase I) 🟡

| Item | Status |
|---|---|
| Independent VLM judge + agreement analysis | Judge adapter (Gemini vision, 3 sampled frames, 1–5 + rationale + safety flags) and `scripts/eval_relevance.py` (Spearman ρ, exact/within-1 agreement, quadratic-weighted κ). Needs a key and human ratings |
| Hindi / Tamil | Danda sentence splitting, Hindi/Tamil sensitivity terms, Noto Devanagari/Tamil caption fonts, Whisper multilingual, `language` request field, scenario files. Needs Gemini (planner translates queries to English) and real voiceovers to verify quality |
| Production numbers | Batch runner records turnaround, `--concurrency`, `--sample-resources` (docker stats). Manifest records stage timings and cost. Numbers are filled in REPORT.md from real runs only |
| ≥ 85 % of scenes rated 4–5 | Not claimable without reviewer ratings |

## 11. Tooling and evidence ✅

| Tool | Purpose |
|---|---|
| `scripts/batch_runner.py` | Folder/file/audio input, unique idempotency keys, polling, downloads, status history, `summary.json/csv`, `ratings_template.csv`, `index.md`, optional docker-stats sampling, never skips failures |
| `scripts/eval_relevance.py` | Relevance metrics and judge-human agreement from the filled ratings sheet |
| `scripts/make_hard_fixtures.py` | Silent, music-only, wrong-type, > 10 min and renamed-video fixtures, plus a ~616-word long narration |
| `scripts/export_openapi.py` | `openapi.yaml` from the live app (parity test) |
| Demo page `/` | Script / upload / URL / scene-list input, live progress, players for each ratio, scene table with clip, licence, score, rule and judge |
| Scenario pack | 10 core (`tests/scenarios/core`), 8 hard (`hard`), 5 audio hard cases (`audio_hard`), 2 multilingual, 1 long |

## 12. Test status

| Suite | Count | Result (local, mock providers) |
|---|---|---|
| unit | 71 | 70 passed, 1 skipped locally (music-only VAD needs faster-whisper, runs in Docker) |
| integration | 22 | passed (incl. batch runner against a live uvicorn server, OpenAPI parity) |
| e2e | 23 | passed (real ffmpeg renders, 3 ratios, swap, provider down, corrupt downloads, concurrency, callback) |

**In Docker (`docker compose run --rm tests`, 2026-11-23):** 116 tests, all passed, exit code 0 (the music-only VAD test also runs here, since faster-whisper is in the image).

## 13. Change log

- 2026-11-22: skeleton, timing core, SRT (25 tests).
- 2026-11-22/23: full pipeline, adapters, API, worker, render, QA, swap, batch runner, docs. Fixed 4 bugs found by tests (see §2, §5, §7).
- 2026-11-23: Redis pinned to 7.2 (BSD-3) after a licence review. Local storage links read `PUBLIC_BASE_URL` per call (the batch-runner test caught a stale base URL).
- 2026-11-23: MinIO images are no longer published, so switched to versitygw (Apache-2.0) and the app creates its own bucket. PyAV 19 broke faster-whisper's decoder; Whisper and VAD now get audio decoded by our ffmpeg. Long ASR sentences are split at phrase boundaries (> 1.5 × target). Mock-TTS jobs use the mock timing model with a MOCK_NARRATION warning. Full suite green in Docker.
