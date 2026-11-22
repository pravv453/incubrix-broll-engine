# IncuBrix Assessment 3 — Script-to-B-roll Video Engine
## Product idea, solution design, features, and API contract

**Assessment:** IncuBrix Engineering Assessment 3  
**Working name:** SceneForge (working name; rename freely)  
**Primary objective:** Convert a script or existing voiceover into a finished, licence-conscious B-roll video, with scene-level footage, narration-synchronised captions, and traceable output metadata.

---

## 1. Executive summary

SceneForge accepts one of three inputs:

1. An uploaded voiceover (`mp3`, `wav`, or `m4a`, up to 10 minutes).
2. A publicly reachable audio URL.
3. Plain text, for which the service generates narration using a configurable text-to-speech provider.

It analyses the narration, divides it into meaningful sentence- or phrase-level scenes, generates visual search briefs for each scene, retrieves candidate stock footage from configured providers, ranks the candidates for semantic relevance and technical suitability, checks rights metadata, and assembles the selected clips into one or more finished videos.

The output package contains:

- `video_16x9.mp4`, `video_9x16.mp4`, and/or `video_1x1.mp4` for requested aspect ratios.
- `captions.srt`, timed to the narration.
- `timeline.json`, containing every scene, its narration timing, selected clip, source, rights/licence details, attribution, AI-generated flag, selection reason, and relevance score.
- `manifest.json`, recording model/provider versions, settings, timings, estimated cost, warnings, and rights information.

The design is deliberately **pipeline-first and deterministic after planning**. Gemini or an open-source LLM may interpret narration and propose scene plans, but ordinary validated Python code controls timestamps, coverage, clip uniqueness, rights gates, retries, and rendering. A model must never be allowed to invent a licence or claim that a clip is commercially cleared.

## 2. Problem statement

Creators currently need to:

- Understand a voiceover sentence by sentence.
- Find footage that expresses the meaning, not merely a matching keyword.
- Download and trim clips to fit the narration.
- Avoid repeating the same clip.
- Reformat footage for landscape, portrait, or square output.
- Add captions and maintain audio/video synchronisation.
- Track the source and licence of every clip.

A naive keyword search produces semantic mismatches. For example, searching for “cloud computing” can return footage of clouds in the sky; “value” can return money even when the narration means personal values. SceneForge uses context-aware visual briefs, candidate ranking, and a recorded selection rationale to reduce this problem.

## 3. Product goals and non-goals

### Goals

- Accept audio and text inputs.
- Generate narration for text input.
- Create scene boundaries from meaning and narration timing.
- Cover every second of the narration with a selected clip or an explicitly reported fallback.
- Choose footage that matches the scene meaning.
- Prevent duplicate source clips within one output video.
- Keep rights metadata and attribution attached to each clip.
- Produce technically consistent MP4 files for every requested aspect ratio.
- Run as a containerised FastAPI service with background jobs and progress.
- Remain testable without paid API keys through mock adapters.

### Non-goals for the first release

- A full nonlinear video editor.
- User accounts, billing, collaborative editing, or a complex timeline UI.
- Training a custom foundation model.
- Unreviewed use of generated realistic people or real-person likenesses.
- Treating a search result's presence in a stock API as proof that every third-party property, trademark, privacy, or model-release issue is cleared.

## 4. User journey

1. The user submits a script or voiceover and selects aspect ratios, visual style, exclusions, and caption settings.
2. The API validates the request and returns `202 Accepted` with a `job_id`.
3. The worker obtains or generates narration, extracts word-level timing, and creates scenes.
4. The scene planner produces a visual brief for each scene.
5. The retrieval layer searches the enabled footage providers.
6. The ranking layer scores candidates, rejects duplicates and unsuitable clips, and selects a rights-eligible candidate.
7. The compositor downloads/normalises selected media and renders each requested aspect ratio.
8. The service creates SRT, timeline, manifest, stores outputs, and marks the job as succeeded.
9. The client polls the job endpoint and downloads the result links.

## 5. Functional features

### 5.1 Input handling

- Accept one of `audio_url`, `audio_file`, `script`, or `scenes`.
- For audio uploads, accept MP3, WAV, and M4A up to 10 minutes.
- Validate file size, MIME type, duration, URL scheme, and URL host policy.
- For text, generate voiceover through a configurable TTS adapter.
- For IncuBrix scene lists, accept `text` and optional `visual_hint` per scene.
- Support `16:9`, `9:16`, and `1:1`.
- Accept a visual style string and `avoid` constraints, such as “no faces” or “no alcohol”.

### 5.2 Transcription and alignment

- Transcribe audio into segments and word-level timestamps.
- Preserve detected language and confidence where available.
- Use voice activity detection to identify silence and validate the transcript.
- Use sentence segmentation plus timing to propose scene boundaries.
- Do not invent word timestamps from the LLM. ASR/alignment is the source of timing truth.
- If timestamps are missing or unreliable, use a forced-alignment adapter or a documented fallback and record a warning.

### 5.3 Semantic scene planner

For each scene, generate:

- `scene_id`
- `text`
- `start_s` and `end_s`
- `visual_concept`
- `primary_search_query`
- `alternative_search_queries`
- `must_include`
- `must_avoid`
- `visual_style`
- `preferred_orientation`
- `sensitivity`
- `planning_reason`

The planner should describe observable visuals. It should avoid vague queries such as “success” and instead use concrete scenes such as “small shop owner checking daily sales on a laptop in a bright retail store”. For abstract narration, use metaphorical but non-misleading imagery and label it as illustrative.

### 5.4 Footage retrieval

- Search each configured provider through a common adapter interface.
- Retrieve a small, bounded candidate set per scene.
- Keep provider IDs, source page URLs, download URLs, creator/author, duration, dimensions, orientation, and licence/attribution metadata.
- Download only candidates that pass provider and rights policy.
- Cache metadata and downloaded media using a content hash.
- Never scrape a stock website or bypass provider rate limits.
- A provider failure must not terminate the entire job if another eligible provider or a clearly disclosed fallback is available.

**Provider strategy:** use a provider abstraction. Pixabay may be enabled where its current API terms and account permissions support the intended workflow. Pexels can be an optional adapter only if valid API access exists; current public Pexels API information says new API key issuance is paused, and its terms restrict use of content to build a similar/competing service. Before enabling either source for a commercial product, review the current provider terms, API policy, asset-level restrictions, and any necessary releases. Do not represent an API result as universally rights-cleared.

### 5.5 Candidate ranking

Rank candidates using a transparent weighted score. Initial configurable formula:

`score = 0.45 * semantic_similarity + 0.20 * visual_concept_match + 0.10 * duration_fit + 0.10 * aspect_fit + 0.10 * quality_score + 0.05 * rights_completeness`

All component scores are normalised to `[0, 1]`. Rights eligibility is a **hard gate**, not just a score: a candidate without sufficient licence/source evidence is rejected even if its relevance score is high.

- Semantic similarity: sentence/visual brief against provider title, tags, and description using an embedding model.
- Visual concept match: optional vision-language model scoring of sampled frames against the scene brief.
- Duration fit: prefer clips long enough to cover the scene or safely loop/cut without obvious repetition.
- Aspect fit: prefer footage that can be cropped to the target ratio without losing the subject.
- Quality: resolution, frame rate, motion, and decode checks.
- Rights completeness: presence of source page, provider, licence identifier/URL, and attribution fields required by policy.

The weights are starting defaults, not claimed evaluation results. Tune them with a labelled validation set and store the active weights in the manifest.

### 5.6 Duplicate prevention

- Use provider asset ID as the primary duplicate key.
- Also compute a perceptual hash for downloaded clips to catch the same footage returned under different IDs.
- Enforce uniqueness within each rendered video.
- A clip may be used in different aspect-ratio outputs for the same scene, because those are alternate renders of the same video.
- If all eligible candidates are exhausted, broaden the search query or use another provider. If no eligible clip remains, use an approved neutral fallback or fail the scene with a clear warning; never silently reuse a clip.

### 5.7 Rights and safety gate

Every selected clip must carry a rights record. At minimum:

- Provider name.
- Provider asset ID.
- Source/asset page URL.
- Licence name or identifier.
- Licence URL or provider terms reference.
- Creator/author and attribution text, if required or available.
- Retrieval timestamp.
- Any restrictions or warnings returned by the provider.
- `commercial_use_allowed` as a policy decision with evidence and review status, not an LLM guess.

Reject assets when rights metadata is absent, terms are incompatible, or a required permission cannot be established. Record the rejection reason.

Safety rules:

- Do not generate realistic faces of real people.
- Do not invent or fabricate brand logos.
- Do not imply endorsement by a person or brand shown in stock footage.
- For named people or brands, use neutral illustrative footage unless the user supplies authorised material and the system can document the rights.
- For sensitive or sad news, use respectful, non-sensational visuals.
- Apply `avoid` constraints to both retrieval queries and post-retrieval checks.
- If safety cannot be established, use a neutral fallback or report a scene warning.

### 5.8 Video rendering

- Use FFmpeg as the primary rendering engine.
- Normalise all selected clips to target width/height, 30 fps, a consistent pixel format, and H.264 video.
- Normalise audio to AAC and ensure the narration is the primary audio track.
- Trim each clip to its assigned scene duration.
- Use crop/scale/pad rules per aspect ratio; avoid stretching.
- Prefer subject-aware crop metadata if available; otherwise use centre crop and record the limitation.
- Concatenate scenes in timeline order.
- Ensure output duration matches narration duration within one frame where technically achievable.
- Detect black/frozen frames and verify audio/video duration and sync.
- Burn captions when requested; also emit SRT when requested.
- If music is added in a later version, it must be rights-cleared, mixed below speech, and included in the manifest.

### 5.9 Captions

- Generate SRT from ASR word timings grouped into readable phrases.
- Keep caption timestamps tied to narration, not to footage durations.
- Use a configurable maximum characters per line and words per caption.
- Burn captions into video only when `captions.burn_in=true`.
- Always preserve the SRT separately when `captions.srt=true`.

### 5.10 Job progress and resilience

Suggested stages and progress bands:

| Stage | Progress |
|---|---:|
| Validating input | 0–5 |
| Generating narration / transcription | 5–20 |
| Scene planning | 20–30 |
| Searching and ranking footage | 30–60 |
| Downloading and normalising clips | 60–75 |
| Rendering outputs | 75–92 |
| Validating and packaging | 92–98 |
| Uploading and completion | 98–100 |

Progress must be monotonic. Retry transient provider/network failures with bounded exponential backoff and jitter. Provider adapters must have timeouts and circuit-breaker behaviour. A failed provider should be skipped while the job continues with another configured provider. Persist job state so a worker restart does not lose the job.

---

## 6. API design

All routes are under `/api/v1/`. The assessment explicitly requires `POST /api/v1/broll/jobs` and `GET /api/v1/jobs/{job_id}`. The remaining routes below are proposed supporting routes to make the service usable and testable.

### 6.1 `POST /api/v1/broll/jobs` — create a B-roll job

Accept either multipart upload or JSON with a URL/text/scenes input. For multipart, send structured options in an `options` JSON field and the audio in `audio_file`.

**JSON request example:**

```json
{
  "audio_url": "https://example.com/voiceover.mp3",
  "script": null,
  "scenes": null,
  "voice": "en-IN-female",
  "aspect_ratios": ["16:9", "9:16"],
  "style": "bright, modern, real-world footage",
  "avoid": ["people's faces", "alcohol"],
  "captions": {
    "burn_in": true,
    "srt": true
  },
  "callback_url": null
}
```

Only one primary input is allowed. `scenes` is a list of `{ "text": "...", "visual_hint": "..." }` objects and may be used instead of `audio_url` or `script`.

Headers:

- `Idempotency-Key`: required for POST; same key and same request returns the same job.
- `X-Correlation-Id`: optional client correlation ID; server creates one if absent.

Response: `202 Accepted`

```json
{
  "job_id": "broll_01J...",
  "status": "queued",
  "progress": 0,
  "status_url": "/api/v1/jobs/broll_01J...",
  "created_at": "2026-11-19T10:00:00Z"
}
```

Validation errors use the standard error shape described below.

### 6.2 `GET /api/v1/jobs/{job_id}` — job status and result

Returns `queued`, `running`, `succeeded`, or `failed`, progress from 0 to 100, stage, warnings, and result links when complete.

```json
{
  "job_id": "broll_01J...",
  "status": "succeeded",
  "stage": "completed",
  "progress": 100,
  "created_at": "2026-11-19T10:00:00Z",
  "updated_at": "2026-11-19T10:02:30Z",
  "warnings": [],
  "result": {
    "videos": {
      "16:9": "https://storage.example/jobs/.../video_16x9.mp4",
      "9:16": "https://storage.example/jobs/.../video_9x16.mp4"
    },
    "captions_srt": "https://storage.example/jobs/.../captions.srt",
    "timeline_json": "https://storage.example/jobs/.../timeline.json",
    "manifest_json": "https://storage.example/jobs/.../manifest.json"
  }
}
```

For failed jobs, include `error` with code/message/retryability/details and retain diagnostic logs for the configured retention period.

### 6.3 `POST /api/v1/jobs/{job_id}/cancel` — cancel a job

Proposed convenience endpoint. Cancels queued jobs immediately and asks running workers to stop at a safe checkpoint. Return `202` for cancellation requested or `409` if the job is already complete.

### 6.4 `GET /api/v1/jobs/{job_id}/timeline` — inspect the timeline

Proposed convenience endpoint returning the timeline JSON without downloading the whole result package. Useful for debugging and reviewer inspection.

### 6.5 `POST /api/v1/jobs/{job_id}/scenes/{scene_id}/clip` — replace a scene clip

Advanced-level feature. Accept a provider asset ID or an approved uploaded clip reference. Validate rights and safety, then rerender only the affected scene/segment and affected final output(s). Record the replacement and reason in the timeline history.

### 6.6 `GET /health` and `GET /ready`

- `/health`: process is alive.
- `/ready`: API, queue, database, and object storage are ready to accept work. Return non-200 when critical dependencies are unavailable.

### 6.7 `GET /openapi.json`

FastAPI-generated OpenAPI schema. Ship a checked-in `openapi.yaml` generated from and verified against the running service.

### 6.8 Callback behaviour

If `callback_url` is provided, POST a job-completion event after the terminal state. Sign the raw request body with HMAC-SHA256 and send it in `X-IncuBrix-Signature`. Use a secret configured through environment variables. Apply an allowlist/SSRF policy to callback hosts and never call arbitrary internal IPs.

### 6.9 Standard error format

```json
{
  "error": {
    "code": "UNSUPPORTED_AUDIO_FORMAT",
    "message": "Only MP3, WAV, and M4A audio are accepted.",
    "retryable": false,
    "details": {
      "received_format": "flac"
    }
  }
}
```

Use stable `UPPER_SNAKE_CASE` codes. Suggested codes:

- `INVALID_REQUEST`
- `MISSING_INPUT`
- `MULTIPLE_INPUTS`
- `UNSUPPORTED_AUDIO_FORMAT`
- `AUDIO_TOO_LONG`
- `EMPTY_SCRIPT`
- `INVALID_ASPECT_RATIO`
- `UNSAFE_URL`
- `INPUT_DOWNLOAD_FAILED`
- `TRANSCRIPTION_FAILED`
- `TTS_FAILED`
- `SCENE_PLANNING_FAILED`
- `NO_ELIGIBLE_FOOTAGE`
- `FOOTAGE_PROVIDER_UNAVAILABLE`
- `FOOTAGE_DOWNLOAD_FAILED`
- `RIGHTS_CHECK_FAILED`
- `RENDER_FAILED`
- `OUTPUT_VALIDATION_FAILED`
- `JOB_NOT_FOUND`
- `JOB_ALREADY_COMPLETED`
- `IDEMPOTENCY_CONFLICT`
- `STORAGE_UNAVAILABLE`
- `INTERNAL_ERROR`

---

## 7. Output data contracts

### 7.1 `timeline.json`

The timeline is the source of truth for scene ordering and narration coverage. Store seconds as floating-point values but render using frame boundaries. Use a stable `scene_id`.

Example:

```json
{
  "job_id": "broll_01J...",
  "duration_s": 12.4,
  "fps": 30,
  "scenes": [
    {
      "scene_id": "scene_001",
      "text": "Small businesses are adopting digital payments.",
      "start_s": 0.0,
      "end_s": 4.2,
      "clip": {
        "provider": "pixabay",
        "asset_id": "example-id",
        "source_url": "https://provider.example/video/example",
        "downloaded_object": "clips/scene_001.mp4",
        "licence": {
          "name": "Provider Content License",
          "url": "https://provider.example/license",
          "commercial_use_allowed": true,
          "evidence_status": "provider_terms_checked"
        },
        "attribution": {
          "required": false,
          "text": "Example Creator via Provider",
          "url": "https://provider.example/video/example"
        },
        "is_ai_generated": false
      },
      "visual_brief": "Shop owner accepting a contactless payment at a counter.",
      "selection": {
        "score": 0.86,
        "score_components": {
          "semantic_similarity": 0.91,
          "visual_concept_match": 0.82,
          "duration_fit": 0.90,
          "aspect_fit": 0.80,
          "quality_score": 0.85,
          "rights_completeness": 1.0
        },
        "reason": "The clip depicts a shop owner accepting a digital payment, matching the narration context.",
        "rank": 1
      },
      "warnings": []
    }
  ]
}
```

The example values are illustrative only. Never write a rights status as `commercial_use_allowed: true` unless the configured rights policy has sufficient evidence. For unknown or unverified rights, reject the clip rather than using this example value.

### 7.2 `manifest.json`

Include:

- Job ID and creation/completion timestamps.
- Application version and Git commit.
- Model provider, model IDs, versions, and settings.
- ASR, alignment, scene-planning, embedding, reranking, TTS, and rendering provider information.
- Footage providers queried and provider failures.
- Requested aspect ratios and output encoding settings.
- Random seed where applicable.
- Stage timings and total turnaround.
- Estimated API, footage, storage, and compute cost; identify unknown costs.
- Warnings and fallback decisions.
- Rights/licence policy version and per-asset rights references.
- Test/validation results: duration, FPS, resolution, audio stream, black/frozen-frame checks, repeat clip count.

### 7.3 `captions.srt`

Use standard SRT numbering and `HH:MM:SS,mmm` timestamps. Captions are generated from ASR timing and must align with the narration.

---

## 8. Definition of done and assessment mapping

### Must-haves — all pass/fail

1. Audio input and text input both work; text input generates narration.
2. Scene changes follow sentence/phrase meaning and timing; every second of narration is covered.
3. Every selected clip has source, licence, attribution, and link; commercial-use eligibility is checked; no watermarked footage; generated footage is flagged.
4. All requested aspect ratios are rendered; clips are normalised; no black/frozen frames or audio drift.
5. No repeated clip in a video; semantic relevance is judged, not keyword overlap alone.
6. Safe output: no generated realistic faces of real people, no invented brand logos, respectful sensitive-topic visuals.
7. Background jobs report progress; a failed footage source does not unnecessarily fail the job.
8. Integration Standard is followed and the live demo remains operational.

### Core level

- All must-haves pass.
- Ten varied test inputs, including tutorials, news summaries, product promos, and stories; varied accents; 20 seconds to 5 minutes.
- Average reviewer relevance of at least 3.5/5 across the ten videos.

### Advanced level

- Two or more footage sources with a measurable selection rule and recorded selection reason.
- Replace one scene's clip and rerender only the affected part, faster than a full render.
- Normalise colour/brightness and use sensible scene pacing.
- Accept IncuBrix scene lists with text and visual hints.

### Outstanding level

- Independent vision-language relevance scoring, compared with human ratings.
- Hindi and Tamil voiceovers as well as English.
- Measured time/cost per finished minute and concurrent job capacity.
- At least 85% of scenes rated 4 or 5 by reviewers.

Do not claim a level until every item in that level is implemented and verified.

---

## 9. Submission and evidence checklist

- [ ] Private GitHub repository with meaningful commit history.
- [ ] `README.md` with setup, architecture, tests, batch runner, and settings.
- [ ] `openapi.yaml` matching the running API.
- [ ] `REPORT.md` (2–4 pages) with design choices, limitations, measured numbers, and cost.
- [ ] `SOURCES.md` listing all models, APIs, libraries, datasets, and media sources.
- [ ] `AI_USE.md` listing AI coding tools and how they were used.
- [ ] Test pack with all inputs, outputs, failures, logs, and evidence index.
- [ ] Live demo URL and access token if protected; rate limiting enabled.
- [ ] Unedited demo recording up to 8 minutes: clean run, failure handling, tests, and one code-level design decision.
- [ ] Video pack containing all outputs, timelines, captions, and manifests, including weak/failed results.
- [ ] Completed IncuBrix submission form with evidence links for every claim.

The brief states that claims without evidence score zero and false claims can disqualify the submission. Preserve failed test attempts and report limitations honestly.

---

## 10. Design decisions and risks

| Risk | Mitigation |
|---|---|
| API provider unavailable or API keys paused | Provider adapters, health checks, retries, alternate configured provider, deterministic mock |
| Search results are semantically wrong | Contextual visual briefs, embedding reranking, optional VLM frame scoring, human-labelled validation set |
| Rights metadata is incomplete | Hard rights gate, asset-level evidence, reject unknown assets |
| Duplicate footage | Provider ID + perceptual hash uniqueness |
| Audio/video drift | Frame-based timeline, normalised audio, post-render validation |
| Portrait crop removes subject | Aspect-aware candidate scoring, crop preview/subject region, fallback candidate |
| Abstract narration | Clearly labelled illustrative visual concept; avoid literal misleading footage |
| Named person or brand | Do not fabricate likeness/logos; use neutral visuals unless authorised media is supplied |
| Long render or worker crash | Background jobs, persisted state, stage checkpoints, bounded retries |
| Cost surprises | Provider adapters, per-stage usage/cost metrics, duration limits and rate limits |

**Core product principle:** The system should be explainable. For every scene, a reviewer should be able to answer: what was said, what visual was requested, which clip was selected, why it was selected, where it came from, what licence applies, and how the final timing was produced.
