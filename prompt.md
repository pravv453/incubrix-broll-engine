# Master Agent Prompt — Build IncuBrix Assessment 3 End-to-End

You are a senior staff-level AI/ML engineer, backend architect, media-pipeline engineer, and test engineer. Your task is to implement the complete IncuBrix Engineering Assessment 3: **Script and Voiceover to B-roll Video Engine**.

You must work in the existing repository and environment. Do not merely write a plan or generate disconnected snippets. Inspect the repository first, implement the system in coherent, testable increments, run tests, fix failures, and leave the project in a runnable state.

The product takes a script or voiceover and returns finished, licence-conscious B-roll videos scene by scene, with narration-synchronised captions and full source/licence traceability.

---

## 1. Source of truth and constraints

Read these project documents before coding:

- `idea.md`
- `pipeline.md`
- This `prompt.md`
- The official IncuBrix Assessment 3 brief supplied by the user, if it is available in the workspace.

The official brief is authoritative. If this prompt or the design documents conflict with the assessment brief, follow the brief and update the docs to explain the decision.

### Mandatory assessment requirements

All of these must pass:

1. Both audio input (MP3, WAV, M4A, up to 10 minutes) and text input work. Text input must generate narration.
2. Scenes are created by meaning and timed to the voice. Scene changes fall on sentence/phrase boundaries and every second of narration is covered.
3. Every clip has source, licence, attribution, and link recorded; all selected clips must be eligible for commercial use; no watermarked footage; AI-generated clips are flagged.
4. Every requested aspect ratio is rendered. Clips are normalised for resolution, frame rate, and pixel format. No black frames, frozen frames, or audio drift.
5. No clip is repeated in one video. Footage must match meaning, not just a keyword.
6. Safe output: no AI-generated realistic faces of real people, no invented brand logos, and respectful footage for sensitive topics.
7. Jobs run in the background with progress. If one footage source fails, the job should still finish using another source or a clearly documented fallback.
8. Follow the Integration Standard and keep the live demo working.

### Integration Standard — implement all ten

1. `docker compose up` starts the service and dependencies; one command runs tests without external API keys; provide `.env.example`.
2. `openapi.yaml` uses OpenAPI 3.1 and exactly matches the running API. Routes live under `/api/v1/`.
3. Any operation slower than two seconds is a background job. POST returns 202 and `job_id`; GET `/api/v1/jobs/{job_id}` returns state, progress 0–100, and result links. Optional callback is HMAC-SHA256 signed in `X-IncuBrix-Signature`.
4. Every POST supports `Idempotency-Key`; same key + same request returns the same job and does not execute twice.
5. Errors use `{"error":{"code","message","retryable","details"}}`; codes are `UPPER_SNAKE_CASE` and documented.
6. Settings, paths, model names, and limits come from environment variables/configuration.
7. Inputs work as upload or URL. Outputs are stored in S3-compatible storage (MinIO in Compose is acceptable) and returned as download links.
8. Structured JSON logs include timestamp, level, job_id, and `X-Correlation-Id`; implement `GET /health` and `GET /ready`.
9. Every result has a manifest listing models/providers/versions, settings, seed, timings, cost estimate, warnings, and licence/rights information.
10. A CLI batch runner accepts a file or folder of test inputs, calls the API, and writes all results into one output folder.

### Evaluation realities

- Every must-have is pass/fail.
- Core requires ten varied inputs, 20 seconds to 5 minutes, and average reviewer relevance of at least 3.5/5.
- Advanced requires two footage sources and a measurable selection rule, clip replacement with partial rerender, coherent visual consistency/pacing, and IncuBrix scene-list input.
- Outstanding requires independent vision-language relevance scoring and human agreement analysis, Hindi and Tamil support, production time/cost/concurrency numbers, and at least 85% of scenes rated 4 or 5.
- Do not claim an item is complete unless code and evidence prove it.
- Keep failed attempts and report limitations honestly.

---

## 2. Required technical choices

### Backend and infrastructure

- Python 3.11+ (or a version compatible with pinned dependencies).
- FastAPI + Pydantic.
- Celery workers + Redis for background jobs.
- PostgreSQL for durable job and idempotency state.
- MinIO for local S3-compatible storage.
- FFmpeg + ffprobe for all final media processing.
- Docker and Docker Compose.
- Pytest for unit, integration, and end-to-end tests.
- A batch runner under `scripts/`.

### Model/provider architecture

Implement interfaces and adapters; do not couple application logic to one vendor.

**Speech-to-text**
- Local: `faster-whisper` using a configurable Whisper checkpoint. Word-level timestamps must be enabled and validated.
- Hosted alternative: Gemini transcription adapter if the current Gemini API project/model supports required word-level timing and language behaviour.
- Add a mock ASR adapter for tests.
- If ASR cannot provide reliable word timings, support a forced-alignment adapter or report a lower-confidence fallback. Never fabricate precise timestamps.

**Text-to-speech**
- Gemini TTS adapter for script input, configured through environment variables and the current official SDK.
- Include a mock TTS adapter for tests.
- Do not assume an open-source TTS model is commercially licensed; only add one after checking the exact code, weights, voice, and training-data terms.

**Scene planning / LLM**
- Primary hosted option: Gemini Flash family through the official `google-genai` SDK.
- Use structured output constrained by a Pydantic/JSON Schema response.
- Keep model ID configurable (e.g. `GEMINI_TEXT_MODEL`); check current official model catalogue and availability rather than hard-coding an obsolete ID.
- Open-source local alternative: a configurable instruction model such as Qwen or Llama only after verifying exact checkpoint licence and commercial-use terms.
- LLM output is a proposal. Python validates, normalises, and owns final scene boundaries and timing.

**Embeddings**
- Sentence Transformers adapter.
- Configurable model: `BAAI/bge-small-en-v1.5` for an English baseline or a multilingual E5 model for Hindi/Tamil experiments.
- Verify exact model-card licence and pin revision.

**Visual relevance**
- Core: semantic text embeddings plus metadata/rule-based quality ranking.
- Advanced/Outstanding: sample a few frames and use a vision-language model such as a verified SigLIP/OpenCLIP checkpoint or Gemini multimodal scoring.
- Record sampled timestamps, model, score, and scoring rationale. Do not send full video files to a model unless necessary.

**Footage providers**
- Implement provider interface and deterministic mock provider first.
- Add real stock adapters only after checking current API access, rate limits, caching rules, asset licence, commercial use, attribution, and terms.
- Pixabay may be an optional adapter subject to its API policy and use limits.
- Pexels is optional only if valid API access exists; public documentation may indicate new API key issuance is paused, and current terms restrict using content to create a similar/competing service. Do not make Pexels a hard dependency or assume permission.
- Implement a second provider adapter for an explicitly licensed provider or a local approved asset pack if available.
- Never scrape, bypass rate limits, or use footage with unclear rights.
- A mock provider must make all tests run without keys.

**Media**
- FFmpeg/ffprobe own the final pipeline.
- Use H.264/AAC, 30 fps, 1080-class output, and consistent pixel format such as `yuv420p`.
- Use explicit frame arithmetic and post-render validation.
- Use no copyrighted music unless a commercial licence is recorded.

---

## 3. Implementation principles

1. Inspect the current repository before modifying it. Preserve existing useful work.
2. Write a short implementation checklist and keep it updated.
3. Build a vertical slice early: one input → one scene → one mock clip → one MP4 → timeline/SRT/manifest.
4. Keep each external service behind an adapter selected by config.
5. Every adapter must have a mock implementation.
6. Keep LLM calls out of deterministic timeline and rendering logic.
7. Validate all LLM outputs with Pydantic and reject invalid timestamps/fields.
8. Never trust provider metadata or user URLs.
9. Use safe temp paths and atomic output publishing.
10. Make each job resumable/checkpointed where practical.
11. Do not hide failures or silently substitute manual output.
12. Pin dependencies and document model/API/licence sources.
13. Never put API keys in code, logs, tests, screenshots, or Git.
14. Keep code understandable enough to explain live; avoid unnecessary abstractions.

---

## 4. Detailed implementation tasks

### Phase A — repository inspection and skeleton

- Inspect files, dependencies, runtime, and existing code.
- Create or update the repository structure in `pipeline.md`.
- Implement FastAPI app, settings, logging, exception handling, and Pydantic schemas.
- Add `/health` and `/ready`.
- Add Dockerfile and Compose for API, worker, Redis, PostgreSQL, and MinIO.
- Add `.env.example`.
- Add tests that start the API and verify health endpoints.

### Phase B — jobs, idempotency, and storage

- Implement durable job table with status, progress, stage, timestamps, warnings, result keys, and error details.
- Implement required `POST /api/v1/broll/jobs`.
- Support JSON input and multipart audio upload. For multipart, accept an `options` JSON field and `audio_file`.
- Enforce exactly one primary input: `audio_url`, `script`, or `scenes` (or uploaded audio file).
- Implement `GET /api/v1/jobs/{job_id}`.
- Implement `Idempotency-Key` for every POST. Same key + same canonical request returns same job. Same key + different request returns `409 IDEMPOTENCY_CONFLICT`.
- Persist job state in PostgreSQL; use Redis/Celery for execution.
- Store input and outputs in MinIO. Return signed download links.
- Implement standard JSON error format and stable error codes.
- Add tests for duplicate submissions and concurrent idempotency requests.

### Phase C — input, TTS, ASR, and alignment

- Validate MP3/WAV/M4A, maximum duration 600 seconds, file size, MIME, and actual media stream via ffprobe.
- For `script`, call TTS adapter, save generated narration, then transcribe/align the actual generated audio.
- For `audio_url`, use an SSRF-safe downloader: only allow HTTP(S), resolve and reject private, loopback, link-local, multicast, and metadata IPs, revalidate redirects, set timeouts and size limits.
- For uploaded audio, store it and probe it.
- Implement `faster-whisper` adapter and word timestamp output.
- Add configurable Gemini transcription adapter if supported by the current official API.
- Add VAD checks for silent/music-only input.
- Detect empty transcript and return explicit error/warning.
- Add tests with small fixtures and mocked adapters.

### Phase D — scene planning

- Implement transcript schema with words, segments, language, duration, confidence, and speaker ID if available.
- Build scene planner adapter with Gemini structured output.
- Include an open-source LLM adapter only if model weights/licence can be pinned and used commercially.
- The model should return scene proposals tied to transcript word IDs or existing timestamps, plus visual concepts and search queries.
- Python must validate:
  - scenes are ordered;
  - start < end;
  - no gaps in narration coverage;
  - no unintended overlaps;
  - first start is zero (or within frame tolerance);
  - final end matches narration duration;
  - boundaries are near sentence/phrase boundaries;
  - very short scenes are merged or explicitly justified.
- For `scenes` input, preserve each scene text and use `visual_hint` as a strong retrieval hint while aligning scene durations to the narration.
- For abstract lines, generate illustrative concepts and mark them as illustrative.
- For numbers/statistics, do not invent visual facts.
- Add test cases for “trust is earned slowly”, “sales grew 40% in 2025”, “cloud computing”, “value”, “bank”, “mouse”, and “python”.

### Phase E — footage providers, rights, ranking, and dedup

- Define `FootageProvider.search(query, filters)` and `download(asset)` methods.
- Define a normalised `FootageCandidate` Pydantic model.
- Implement `MockFootageProvider` with deterministic fixture clips and realistic metadata.
- Implement one real provider adapter only after current terms/API credentials are verified.
- Implement a second provider adapter or approved local asset provider for failover and Advanced-level proof.
- Apply a hard rights gate before scoring. Reject missing or unclear licence evidence, watermarked clips, restricted editorial-only clips for commercial output, and unsupported assets.
- Record source URL, provider asset ID, licence identifier/name/URL, creator, attribution, retrieval time, restrictions, and evidence status.
- Do not allow an LLM to decide commercial rights.
- Implement candidate ranking with configurable weights:
  - semantic similarity 0.45;
  - visual match 0.20;
  - duration fit 0.10;
  - aspect fit 0.10;
  - quality 0.10;
  - rights metadata completeness 0.05.
- Treat rights eligibility as a hard gate, not a weighted preference.
- Implement dedup by provider asset ID and perceptual hash.
- Never repeat a clip within one output video.
- If source A fails, try source B; if all fail, use a clearly identified approved fallback or fail with a useful error.
- Test provider timeout, HTTP 429, HTTP 5xx, corrupted download, no results, rights rejection, and duplicates.

### Phase F — render and captions

- Implement frame-based timeline representation.
- Generate SRT from word timestamps and phrase grouping.
- Use FFmpeg to:
  - trim clips;
  - scale/crop/pad to each requested aspect ratio;
  - normalise to 30 fps and `yuv420p`;
  - remove source audio from B-roll unless intentionally mixed;
  - add narration;
  - burn captions when requested;
  - encode H.264/AAC;
  - concatenate scenes without timing drift.
- Render each requested ratio independently.
- Support 16:9, 9:16, and 1:1.
- Make scene pacing sensible; merge very short scenes rather than flashing.
- Add post-render ffprobe and frame checks for dimensions, FPS, codec, duration, audio stream, black frames, and frozen frames.
- Never report success if validation fails.

### Phase G — timeline, manifest, observability

- Generate `timeline.json` with narration text/timing, clip details, source, licence, attribution, AI-generated flag, visual brief, selection reason, relevance score, and warnings.
- Generate `manifest.json` with:
  - app version/Git SHA;
  - model and provider names/versions;
  - settings and seed;
  - stage timings and total turnaround;
  - cost estimates and unknown cost fields;
  - warnings/fallbacks;
  - rights/licence information;
  - validation metrics.
- Add structured JSON logs with timestamp, level, job_id, and correlation ID.
- Add callback support; HMAC-SHA256 signature in `X-IncuBrix-Signature`.
- Validate callback URL against SSRF and host allowlist.
- Add tests for manifest fields, logs, callback signature, and output files.

### Phase H — Advanced features

After all must-haves pass:

- Add a second footage source with a measurable selection/fallback rule.
- Add clip replacement endpoint:
  `POST /api/v1/jobs/{job_id}/scenes/{scene_id}/clip`
- Re-render only the affected segment and recombine without re-rendering unrelated scenes.
- Add colour/brightness normalisation and pacing constraints.
- Support IncuBrix-provided scene lists.
- Record provider selection rationale in `timeline.json`.

### Phase I — Outstanding features

Only attempt after Core is reliable:

- Independent vision-language relevance scoring with human-rating agreement analysis.
- Tamil and Hindi narration/voiceover support with real test data.
- Measure latency, cost per finished minute, peak RAM/GPU, and concurrent job throughput.
- Target at least 85% of scenes rated 4 or 5, and report actual measured results.

---

## 5. API contract to implement

All routes are under `/api/v1/`.

### Create job

`POST /api/v1/broll/jobs`

Headers:
- `Idempotency-Key: <unique-client-key>` (required)
- `X-Correlation-Id: <optional-correlation-id>`

JSON example:

```json
{
  "audio_url": "https://example.com/voiceover.mp3",
  "script": null,
  "scenes": null,
  "voice": "en-IN-female",
  "aspect_ratios": ["16:9", "9:16"],
  "style": "bright, modern, real-world footage",
  "avoid": ["people's faces", "alcohol"],
  "captions": {"burn_in": true, "srt": true},
  "callback_url": null
}
```

Return HTTP 202:

```json
{
  "job_id": "broll_<id>",
  "status": "queued",
  "progress": 0,
  "status_url": "/api/v1/jobs/broll_<id>"
}
```

Multipart:
- `audio_file`: file upload.
- `options`: JSON-encoded request options excluding `audio_url` and `script`.

### Get job

`GET /api/v1/jobs/{job_id}`

Return job status (`queued`, `running`, `succeeded`, `failed`), progress 0–100, current stage, warnings, error details if failed, and result URLs if succeeded.

### Additional proposed routes

- `POST /api/v1/jobs/{job_id}/cancel`
- `GET /api/v1/jobs/{job_id}/timeline`
- `POST /api/v1/jobs/{job_id}/scenes/{scene_id}/clip`
- `GET /health`
- `GET /ready`

### Standard error

```json
{
  "error": {
    "code": "UNSUPPORTED_AUDIO_FORMAT",
    "message": "Only MP3, WAV, and M4A audio are accepted.",
    "retryable": false,
    "details": {}
  }
}
```

Generate and check in `openapi.yaml` from the actual FastAPI app. Do not let the checked-in file drift from the live API.

---

## 6. Required output files

For every successful job, produce:

1. `video_{ratio}.mp4` for each requested ratio, H.264/AAC, 30 fps, 1080-class.
2. `captions.srt` when requested.
3. `timeline.json` with scene-level timing, clip, source, licence, attribution, AI-generated flag, selection reason, and relevance score.
4. `manifest.json` with models/providers/versions, settings, seed, timings, cost estimate, warnings, and rights data.

Keep failed outputs/logs in the test pack. Do not delete evidence of failures.

---

## 7. Test-first acceptance criteria

Create tests before or alongside implementation. At minimum, the test suite must cover:

- Text input produces TTS audio and a finished video.
- Audio upload works for MP3, WAV, M4A.
- Ten-minute duration boundary and over-limit rejection.
- Invalid file, empty script, silent audio, music-only audio.
- Sentence/phrase scene boundaries and complete coverage.
- Abstract narration and ambiguous terms.
- Correct 16:9, 9:16, and 1:1 outputs.
- 30 fps, codec, pixel format, dimensions, audio presence.
- Audio/video duration and sync within one frame.
- No black/frozen frames.
- No duplicate clips in one output.
- Rights-ineligible and unknown-rights candidates are rejected.
- Source metadata and attribution are recorded.
- No realistic generated real-person faces or invented logos.
- Sensitive-topic handling.
- Provider A down while provider B succeeds.
- Corrupt download fallback.
- Two simultaneous job submissions.
- Idempotent duplicate POST.
- Job progress is monotonic and reaches 100 on success.
- Correct standard error JSON.
- Health/readiness endpoints.
- HMAC callback signature.
- Batch runner processes a folder and stores every response/result.
- Compose starts and tests pass with no API keys.

Use deterministic fixtures and mock adapters for the required no-key test path. Real-provider tests should be opt-in and clearly separated from CI.

---

## 8. Batch runner

Implement `scripts/batch_runner.py` with options similar to:

```bash
python scripts/batch_runner.py \
  --input tests/scenarios \
  --api http://localhost:8000 \
  --output test-results \
  --wait \
  --poll-interval 2
```

It should:
- Accept one scenario file or a folder.
- Submit each job with a unique idempotency key.
- Poll until terminal status.
- Download every result.
- Save request, response, job status, logs/diagnostics, and output files.
- Continue after one scenario fails.
- Produce a summary JSON/CSV with success, duration, warnings, and links/paths.
- Never silently omit failed cases.

---

## 9. Documentation and deliverables

Create/update:

- `README.md`: setup, architecture, configuration, API usage, tests, batch runner, troubleshooting.
- `openapi.yaml`: exact OpenAPI 3.1 contract.
- `SOURCES.md`: every model, API, library, dataset, and media source, with version/licence and purpose.
- `AI_USE.md`: AI coding assistants used and how.
- `REPORT.md`: 2–4 pages with considered approaches, chosen design, what works/doesn't, measured metrics, cost per minute, and next steps.
- `tests/` test pack and scenario fixtures.
- `demo/` or `test-results/` evidence for clean run, failure handling, and tests.
- `idea.md`, `pipeline.md`, and `prompt.md` kept consistent with the implementation.

Do not invent model versions, licence status, cost figures, benchmark results, or test outcomes. Use `TBD` until measured.

---

## 10. Work protocol and final response

Work in small, verifiable increments:

1. Inspect repository and report existing structure.
2. Establish skeleton and mock end-to-end flow.
3. Run tests and fix errors.
4. Add real providers behind adapters.
5. Re-run full no-key tests.
6. Run opt-in real-provider tests only when credentials exist.
7. Generate OpenAPI and verify it.
8. Build Docker images and run Compose.
9. Run the batch pack and capture outputs.
10. Update docs and provide a concise completion report.

After each phase, summarise:
- Files changed.
- Commands/tests run.
- Passing/failing tests.
- Known gaps.
- Next phase.

Do not claim the task is finished until the application starts, the mock end-to-end flow renders an MP4, the API and batch runner work, all must-have tests pass, and the output package contains video, captions, timeline, and manifest.

If a required external API key, licence clarification, or model access is missing, implement the adapter and mock path, document the missing prerequisite, and continue with the rest of the system. Never fake a successful provider call or fabricate licence evidence.
