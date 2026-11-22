# IncuBrix Assessment 3 — Technical Pipeline and Architecture

## 1. Architecture goals

Build a production-shaped but assessment-sized service with:

- FastAPI for HTTP APIs and validation.
- Celery + Redis for background processing.
- PostgreSQL for jobs, idempotency records, and metadata.
- MinIO for local S3-compatible object storage.
- FFmpeg/ffprobe for deterministic media processing.
- Pluggable ASR, TTS, LLM, embedding, vision, and stock-footage providers.
- Mock adapters so tests run without external API keys.
- Structured logs, progress updates, retries, and output manifests.

The architecture separates probabilistic AI planning from deterministic media operations. Models produce structured suggestions; validated Python services enforce timing, rights, uniqueness, and output correctness.

## 2. High-level system diagram

```mermaid
flowchart TD
    U[Client / Demo UI / Batch Runner] --> API[FastAPI]
    API --> V[Request Validation + Idempotency]
    V --> DB[(PostgreSQL)]
    V --> Q[Redis Queue]
    API --> S3[(MinIO / S3 Storage)]
    Q --> W[Celery Worker]

    W --> IN[Input Resolver]
    IN --> TTS[TTS Adapter - text only]
    IN --> ASR[ASR + Word Alignment]
    TTS --> ASR
    ASR --> SP[Scene Planner]
    SP --> LLM[Gemini or Open-source LLM]
    SP --> RET[Footage Retrieval Adapters]
    RET --> P1[Provider A]
    RET --> P2[Provider B]
    P1 --> RANK[Candidate Ranker + Rights Gate]
    P2 --> RANK
    RANK --> EMB[Embedding / VLM Scoring]
    EMB --> DL[Safe Download + Media Probe]
    DL --> NORM[FFmpeg Normalisation]
    NORM --> TL[Timeline Builder]
    TL --> RENDER[FFmpeg Renderer]
    RENDER --> QA[Output QA]
    QA --> PKG[Captions + Timeline + Manifest]
    PKG --> S3
    PKG --> DB
    API --> STATUS[Job Status API]
    STATUS --> DB
```

## 3. Recommended repository structure

```text
incubrix-broll-engine/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── routes/
│   │   │   ├── broll.py
│   │   │   ├── jobs.py
│   │   │   └── health.py
│   │   └── dependencies.py
│   ├── core/
│   │   ├── config.py
│   │   ├── errors.py
│   │   ├── logging.py
│   │   ├── security.py
│   │   └── idempotency.py
│   ├── schemas/
│   │   ├── requests.py
│   │   ├── jobs.py
│   │   ├── scenes.py
│   │   ├── timeline.py
│   │   └── manifest.py
│   ├── services/
│   │   ├── job_service.py
│   │   ├── input_service.py
│   │   ├── transcription_service.py
│   │   ├── alignment_service.py
│   │   ├── scene_planner.py
│   │   ├── retrieval_service.py
│   │   ├── ranking_service.py
│   │   ├── rights_service.py
│   │   ├── dedup_service.py
│   │   ├── timeline_service.py
│   │   ├── caption_service.py
│   │   ├── render_service.py
│   │   ├── validation_service.py
│   │   └── manifest_service.py
│   ├── adapters/
│   │   ├── asr/
│   │   │   ├── base.py
│   │   │   ├── faster_whisper.py
│   │   │   ├── gemini_transcribe.py
│   │   │   └── mock.py
│   │   ├── tts/
│   │   │   ├── base.py
│   │   │   ├── gemini_tts.py
│   │   │   └── mock.py
│   │   ├── llm/
│   │   │   ├── base.py
│   │   │   ├── gemini.py
│   │   │   ├── local_transformers.py
│   │   │   └── mock.py
│   │   ├── embeddings/
│   │   │   ├── base.py
│   │   │   ├── sentence_transformers.py
│   │   │   └── mock.py
│   │   ├── vision/
│   │   │   ├── base.py
│   │   │   ├── siglip.py
│   │   │   └── gemini_vision.py
│   │   └── footage/
│   │       ├── base.py
│   │       ├── pixabay.py
│   │       ├── pexels.py
│   │       ├── licensed_provider.py
│   │       └── mock.py
│   ├── workers/
│   │   ├── celery_app.py
│   │   └── tasks.py
│   ├── storage/
│   │   ├── base.py
│   │   └── s3_storage.py
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py
│   │   └── repositories.py
│   └── utils/
│       ├── ffmpeg.py
│       ├── urls.py
│       ├── timecode.py
│       └── hashes.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   ├── fixtures/
│   └── scenarios/
├── scripts/
│   └── batch_runner.py
├── docker/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── openapi.yaml
├── SOURCES.md
├── AI_USE.md
└── README.md
```

## 4. Stack and model choices

Use provider interfaces so each model can be changed by environment configuration. Do not hard-code a model ID throughout business logic.

### 4.1 Speech recognition and word timestamps

**Primary local option: `faster-whisper` with Whisper `small` or `medium`**

- Open-source implementation of Whisper inference using CTranslate2.
- Runs locally and avoids per-minute transcription API costs.
- Select `small` for CPU/low-memory development and `medium` or `large-v3` when GPU resources permit.
- Whisper is multilingual and supports language detection.
- `faster-whisper` can expose segment timestamps; word-level timestamps should be enabled and tested.
- Use a separate forced-alignment stage if the chosen ASR output is not sufficiently accurate at word boundaries.

**Gemini option:** configurable Gemini transcription model through the official Google Gen AI SDK. Current Gemini model catalogue includes a Gemini transcription model with speaker diarisation and word timestamps; verify exact availability, limits, and SDK support in the target project before choosing it.

Recommended interface:

```python
class ASRProvider(Protocol):
    async def transcribe(self, audio_path: str, language: str | None = None) -> Transcript:
        ...
```

The `Transcript` contract should contain detected language, segments, words, timestamps, speaker IDs where available, and confidence where available.

### 4.2 Voice activity detection and alignment

- **Silero VAD**: detect speech and silence regions; useful for sanity checks and alignment diagnostics.
- **WhisperX / wav2vec2 forced alignment**: optional word alignment path if word timings from ASR are insufficient.
- Treat speaker diarisation as best-effort for this assessment; the must-have is narration timing, not speaker-specific editing.
- Store the alignment provider and version in the manifest.

Verify the exact model/code licence before shipping. Pin model revisions and package versions.

### 4.3 Text-to-speech

**Preferred hosted option: Gemini TTS adapter**, using a currently supported Gemini TTS model ID configured via `GEMINI_TTS_MODEL`.

- Text input must produce a real audio file.
- Voice name, language, speaking style, sample rate, and output format should be configuration/request parameters.
- Persist the generated audio as the canonical narration asset and transcribe/align that generated file, rather than assuming the TTS text timing.
- Store provider/model/voice/settings in the manifest.

**Local option:** use a self-hosted TTS model only after verifying the model weights, code, voice, and dataset licences for commercial use. Do not assume that a popular TTS checkpoint is commercially usable.

### 4.4 Scene planning and structured reasoning

**Primary hosted option: Gemini Flash family**

- Use `google-genai` Python SDK.
- Use Gemini structured output / JSON Schema or Pydantic response models for deterministic parsing.
- Set `GEMINI_TEXT_MODEL` through configuration. A current Flash model can be selected after checking the official model catalogue and project availability.
- Gemini produces scene boundaries as references to existing transcript word indices/timestamps; Python validates and finalises them.
- It produces visual concepts and search phrases, not final rights decisions.

**Open-source fallback options:**

- `Qwen2.5-7B-Instruct` or a newer commercially compatible instruct model for local structured planning.
- `Llama 3.1 8B Instruct` as another possible local model, subject to the exact licence and usage restrictions.
- Use an adapter and keep the default model choice configurable. Confirm the current model-card licence, acceptable use policy, and commercial suitability before shipping. Do not treat “open weights” as synonymous with unrestricted commercial use.

Recommended interface:

```python
class ScenePlanner(Protocol):
    async def plan(
        self,
        transcript: Transcript,
        style: str,
        avoid: list[str],
        supplied_scenes: list[InputScene] | None = None,
    ) -> list[PlannedScene]:
        ...
```

### 4.5 Text embeddings and semantic retrieval

**Open-source candidate models:**

- `BAAI/bge-small-en-v1.5`: lightweight English retrieval embeddings.
- `intfloat/multilingual-e5-base`: multilingual option for English, Hindi, and Tamil experiments.
- `sentence-transformers/all-MiniLM-L6-v2`: lightweight baseline for a quick prototype, subject to its model-card licence.

Use Sentence Transformers to embed:
- Scene narration.
- Visual concept.
- Search query.
- Provider title/tags/description.

For each candidate, compute cosine similarity and retain the score components. For multilingual support, select a multilingual embedding model and evaluate with actual Tamil/Hindi examples rather than assuming parity.

### 4.6 Visual relevance scoring

**Core release:** text-embedding ranking over provider metadata + rule-based technical suitability.

**Advanced/Outstanding option:** score sampled candidate frames against the visual brief using one of:

- OpenCLIP/SigLIP-compatible vision-language encoder, after verifying the exact weights and licence.
- Gemini multimodal model for independent scene-to-frame relevance scoring.

Do not send every full-resolution video to a vision model. Sample a small number of frames per candidate (e.g., beginning, middle, and end), resize them, and score those frames. Store model, sampled timestamps, raw score, normalised score, and any warning.

### 4.7 Video and audio processing

- FFmpeg and ffprobe for decoding, trimming, scaling, cropping, frame-rate conversion, audio normalisation, subtitles, concatenation, and metadata inspection.
- Prefer direct FFmpeg filter graphs over a high-level wrapper for exact timing and reproducibility.
- MoviePy may be used for prototypes, but FFmpeg should own the final render path.
- Use H.264/AAC, 30 fps, and a consistent pixel format such as `yuv420p`.
- Keep frame arithmetic explicit: `frame = round(seconds * fps)`; avoid accumulating floating-point drift across scene joins.

Check the licence of the exact FFmpeg build and any bundled codecs in the shipped container.

### 4.8 Footage providers

Create a common `FootageProvider` interface. Initial adapters:

1. **Pixabay API**: optional provider after reviewing current API terms, rate limits, caching rules, commercial-use scope, and attribution requirements. Download media to controlled storage before rendering. Respect its API rate limits and caching requirements; avoid systematic mass-download behaviour.
2. **Pexels API**: optional only if valid API credentials/access are available. Public Pexels API information currently indicates new API key issuance is paused. Its terms also restrict compiling content to build a similar or competing service. Obtain provider clarification/permission before relying on it for this product.
3. **Licensed commercial provider adapter**: implement the interface for a provider whose contract explicitly permits this automated commercial use.
4. **Mock provider**: deterministic local fixture clips for no-key tests.

Never promise commercial clearance based only on a provider name. Keep the provider's terms URL and asset-level evidence with each result. Some rights (model releases, trademarks, privacy, editorial-only restrictions) may need separate review.

### 4.9 Storage and background jobs

- PostgreSQL: job records, idempotency keys, provider metadata, timeline references, manifest references, and stage status.
- Redis: Celery broker and result backend (or use Redis only as broker and persist authoritative job state in PostgreSQL).
- MinIO: local S3-compatible input/output storage.
- Production object storage can be swapped through a storage adapter.
- Use signed download URLs with an expiry or authenticated download endpoints.

## 5. End-to-end execution pipeline

### Stage 0 — Request validation

- Validate exactly one primary input.
- Validate audio extensions/MIME/duration or non-empty script/scenes.
- Validate aspect ratios against `{16:9, 9:16, 1:1}`.
- Validate `avoid`, style, callback URL, and voice.
- Enforce upload and URL size limits.
- URL fetcher must block private IP ranges, localhost, link-local addresses, metadata endpoints, and redirect-to-private-IP SSRF.
- Store input in object storage and compute SHA-256.

### Stage 1 — Narration creation / resolution

If script:
1. Validate and normalise text.
2. Call configured TTS provider.
3. Save narration audio.
4. Record TTS model, voice, language, cost, and duration.

If audio URL or upload:
1. Fetch/stream safely or use uploaded file.
2. Probe media and verify supported audio stream.
3. Convert to canonical PCM WAV for ASR if needed.

### Stage 2 — Transcription and alignment

1. Run ASR.
2. Obtain word-level timestamps.
3. Run VAD checks.
4. Validate monotonic timestamps and bounds.
5. Detect empty/silent/music-only input.
6. Save transcript JSON.

If word timestamps are absent, run forced alignment. If alignment still fails, fail clearly or use a documented lower-confidence segment-level fallback; do not silently fabricate precise timestamps.

### Stage 3 — Scene segmentation

1. Split transcript into sentences using punctuation and text segmentation.
2. Merge very short sentences when they form one visual idea.
3. Split long sentences at phrase boundaries when useful.
4. Ask Gemini/local LLM for semantic grouping and visual briefs using transcript word IDs.
5. Validate no gaps or overlaps in coverage unless explicitly permitted.
6. Convert boundaries to frame-aligned start/end times.
7. Enforce configurable minimum scene duration; merge short scenes rather than generating flashing cuts.
8. Ensure first scene begins at 0 and final scene ends at narration duration (within frame tolerance).

A scene planner may suggest boundaries, but Python owns the final contiguous coverage map.

### Stage 4 — Visual brief generation

For every scene:
- Extract core subject, action, setting, context, and mood.
- Produce one primary and multiple alternate search queries.
- Avoid ambiguous single-word queries.
- Include user style and avoid terms.
- For abstract concepts, propose a clearly illustrative visual metaphor.
- For statistics, prefer charts, relevant work scenes, or neutral data visuals; do not fabricate a specific event.
- For named people/brands, do not generate a face/logo; use generic visuals unless authorised source material is supplied.

### Stage 5 — Retrieval

For each scene:
1. Query provider A.
2. Query provider B if configured or if provider A has too few eligible candidates.
3. Normalise provider response to a common `FootageCandidate` schema.
4. Deduplicate by provider asset ID and URL.
5. Filter unsupported formats, watermarks, insufficient duration/resolution, and rights-ineligible assets.
6. Cache provider responses according to provider policy.

Provider failures should be recorded and retried with bounded backoff. If one source is down, continue with the other source.

### Stage 6 — Ranking and selection

1. Embed scene text and candidate metadata.
2. Calculate semantic similarity.
3. Optionally calculate visual similarity from sampled frames.
4. Calculate duration, aspect, and technical-quality scores.
5. Apply rights gate and avoid/safety filters.
6. Rank eligible candidates.
7. Choose the best candidate not already used in this video.
8. Record score components and a human-readable selection reason.
9. If no suitable candidate exists, broaden search or use a pre-approved fallback; otherwise fail that scene explicitly.

Suggested score (configurable):

`0.45 semantic + 0.20 visual + 0.10 duration + 0.10 aspect + 0.10 quality + 0.05 rights metadata completeness`

Rights eligibility is a hard filter before scoring, not a soft preference.

### Stage 7 — Download and normalise

- Download only selected clips.
- Validate response MIME type, file size, checksum, and decode.
- Store original downloaded asset and metadata.
- Trim a subclip with enough source duration.
- Normalise dimensions, FPS, pixel format, and audio stream handling.
- Generate target-specific crops for each aspect ratio.
- Detect corrupted or unsupported clips and fall back to the next ranked candidate.

### Stage 8 — Timeline and captions

- Build the authoritative timeline from scene boundaries.
- Associate each selected clip with the scene's exact start/end.
- Generate SRT from word-level transcript timing.
- Keep captions independent from clip boundaries.
- Record source/licence/attribution, AI-generated status, relevance scores, and warnings.
- Validate no duplicate clips per output.

### Stage 9 — Rendering

For each requested aspect ratio:
1. Build FFmpeg filter graph.
2. Trim and scale/crop/pad each scene.
3. Set constant frame rate and pixel format.
4. Concatenate in scene order.
5. Add narration audio.
6. Add subtitles if burn-in is requested.
7. Encode H.264/AAC.
8. Write output to temporary path, then atomically publish to object storage after validation.

### Stage 10 — QA and manifest

Use ffprobe and frame/audio checks to verify:
- File exists and is decodable.
- Correct output aspect ratio and target dimensions.
- H.264/AAC streams.
- 30 fps output.
- Duration matches narration within one frame where possible.
- Audio stream exists and is synchronised.
- No black frames or frozen frames.
- No duplicate clip IDs.
- Every scene is covered.
- Every clip has a complete rights record and source link.
- Captions parse and fall within narration duration.

Create `manifest.json`, upload all outputs, update the job to succeeded, and emit callback if configured.

## 6. Core data models

Suggested Pydantic models (simplified):

```python
from typing import Literal
from pydantic import BaseModel, Field, HttpUrl

AspectRatio = Literal["16:9", "9:16", "1:1"]

class CaptionOptions(BaseModel):
    burn_in: bool = True
    srt: bool = True

class InputScene(BaseModel):
    text: str = Field(min_length=1)
    visual_hint: str | None = None

class BrollJobRequest(BaseModel):
    audio_url: HttpUrl | None = None
    script: str | None = None
    scenes: list[InputScene] | None = None
    voice: str = "en-IN-female"
    aspect_ratios: list[AspectRatio] = ["16:9"]
    style: str = "bright, modern, real-world footage"
    avoid: list[str] = []
    captions: CaptionOptions = CaptionOptions()
    callback_url: HttpUrl | None = None

class Word(BaseModel):
    text: str
    start_s: float
    end_s: float
    confidence: float | None = None
    speaker_id: str | None = None

class Transcript(BaseModel):
    language: str | None = None
    duration_s: float
    words: list[Word]

class PlannedScene(BaseModel):
    scene_id: str
    text: str
    start_s: float
    end_s: float
    visual_concept: str
    search_queries: list[str]
    avoid_terms: list[str] = []
```

Use `default_factory` for mutable defaults in actual code. The above is illustrative schema structure, not production-ready validation.

## 7. API and worker lifecycle

### API routes

- `POST /api/v1/broll/jobs` — create job; returns 202 with job ID.
- `GET /api/v1/jobs/{job_id}` — status, progress, and result URLs.
- `POST /api/v1/jobs/{job_id}/cancel` — proposed optional cancellation endpoint.
- `GET /api/v1/jobs/{job_id}/timeline` — proposed timeline inspection endpoint.
- `POST /api/v1/jobs/{job_id}/scenes/{scene_id}/clip` — proposed Advanced clip replacement endpoint.
- `GET /health` — process liveness.
- `GET /ready` — dependency readiness.

### Idempotency

For every POST:
- Require `Idempotency-Key`.
- Store key + canonical request hash + job ID.
- Same key and same request returns same job.
- Same key with a different request returns `409 IDEMPOTENCY_CONFLICT`.
- Enforce unique constraint in database to handle concurrent requests.

### Background job states

`queued → running → succeeded | failed | cancelled`

Persist:
- Current stage.
- Progress percentage.
- Attempt count.
- Start/end times.
- Error code/details.
- Warnings.
- Result object keys.

### Retry policy

- Retry transient network timeouts, 429, and 5xx errors with bounded exponential backoff and jitter.
- Honour provider `Retry-After` headers.
- Do not retry invalid requests, unsupported media, licence rejection, or deterministic render validation errors without changing inputs.
- Make downloads and rendering idempotent using deterministic job/scene paths and temporary files.

## 8. Rendering and frame math

For output FPS `F=30`, map time to frames:

`start_frame = round(start_s * F)`

`end_frame = round(end_s * F)`

`scene_frames = end_frame - start_frame`

The total rendered frame count should match the narration's expected frame count within one frame. Keep a single frame-rate policy for all scenes. Avoid repeated float additions; calculate each boundary from absolute time.

Aspect ratio target examples:

| Ratio | 1080-class canvas |
|---|---|
| 16:9 | 1920 × 1080 |
| 9:16 | 1080 × 1920 |
| 1:1 | 1080 × 1080 |

Use scale-to-cover + crop when subject-safe, or scale-to-fit + pad when cropping would remove important content. Record which policy was used per clip and ratio.

## 9. Security, rights, and operational controls

- Store API keys only in environment variables or secret management.
- Never log secrets, signed URLs, or raw callback secrets.
- Restrict input and callback URL fetching to safe public hosts/IPs.
- Limit upload size, audio duration, number of aspect ratios, scene count, provider calls, and concurrent jobs.
- Use signed URLs with short expiry for downloads.
- Keep an allowlist of footage providers and permitted licence policies.
- Treat provider metadata as untrusted input.
- Keep rights evidence in the manifest and timeline.
- Avoid any asset with unclear commercial-use eligibility.
- Pin Python dependencies and model revisions; document licences in `SOURCES.md`.
- Use non-root containers and read-only filesystem where practical.
- Rate-limit public API endpoints and protect the demo with a token.

## 10. Environment variables

```dotenv
APP_ENV=development
APP_VERSION=0.1.0
API_PREFIX=/api/v1

DATABASE_URL=postgresql+psycopg://app:app@postgres:5432/broll
REDIS_URL=redis://redis:6379/0

S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=minio
S3_SECRET_KEY=change-me
S3_BUCKET=broll
S3_REGION=us-east-1
S3_SECURE=false

ASR_PROVIDER=faster_whisper
ASR_MODEL=small
TTS_PROVIDER=gemini
GEMINI_API_KEY=
GEMINI_TEXT_MODEL=gemini-3.8-flash
GEMINI_TTS_MODEL=
GEMINI_TRANSCRIBE_MODEL=
LLM_PROVIDER=gemini
EMBEDDING_PROVIDER=sentence_transformers
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
VISION_PROVIDER=disabled

FOOTAGE_PROVIDERS=pixabay,mock
PIXABAY_API_KEY=
PEXELS_API_KEY=
FOOTAGE_MAX_CANDIDATES=12
FOOTAGE_REQUEST_TIMEOUT_S=20

OUTPUT_FPS=30
OUTPUT_WIDTH_LANDSCAPE=1920
OUTPUT_HEIGHT_LANDSCAPE=1080
OUTPUT_WIDTH_PORTRAIT=1080
OUTPUT_HEIGHT_PORTRAIT=1920
OUTPUT_WIDTH_SQUARE=1080
OUTPUT_HEIGHT_SQUARE=1080

MAX_AUDIO_DURATION_S=600
MAX_UPLOAD_MB=200
MAX_CONCURRENT_JOBS=2
CALLBACK_HMAC_SECRET=
DEMO_ACCESS_TOKEN=
```

The example Gemini text model is a configuration default, not a permanent promise of availability. Verify current model IDs and project access before deployment. Never commit real secrets.

## 11. Docker Compose services

Minimum services:

- `api`: FastAPI + Uvicorn.
- `worker`: Celery worker with FFmpeg installed.
- `redis`: queue broker.
- `postgres`: persistent job metadata.
- `minio`: S3-compatible local storage.
- Optional `flower`: worker observability during development.

`docker compose up --build` should start the service. Tests should use mock adapters and local fixture media so no external API keys are required.

## 12. Testing plan

### Unit tests

- Input validation and mutually exclusive inputs.
- Scene coverage/no gaps/no overlaps.
- Frame conversion and duration tolerance.
- Candidate ranking and score normalisation.
- Rights-gate rejection.
- Duplicate detection.
- SRT generation and timestamp formatting.
- Idempotency key behaviour.
- Error mapping and retry classification.

### Integration tests

- FastAPI request to queued job.
- Celery worker status transitions.
- MinIO upload/download.
- Mock ASR/TTS/LLM/footage provider.
- FFmpeg render of fixture clips.
- Callback HMAC signature.
- Provider failure and fallback.

### End-to-end tests

- Script → mock TTS → scenes → mock footage → MP4 + SRT + timeline + manifest.
- Audio upload → ASR → scenes → footage → final package.
- Multiple aspect ratios.
- Empty/silent audio and invalid file types.
- 5-minute narration.
- Provider A unavailable while provider B succeeds.
- Candidate duplication and rights rejection.
- Portrait crop edge case.
- Concurrent job submissions.

### Core-level evaluation set

Prepare ten distinct inputs (20 seconds to 5 minutes) across tutorial, news summary, product promo, story, abstract concept, numerical/statistical narration, named brand/person, and varied accents. Rate each scene 1–5 for relevance. Report average relevance and coverage honestly.

### Metrics

- Turnaround: cold and warm processing time for 60 seconds of narration.
- Cost: estimated cost per finished minute, including TTS, LLM, footage, compute, and storage.
- Coverage: narration duration covered by clips rated at least 3/5.
- Relevance: human scene ratings; independent model score if implemented.
- Reliability: successful renders / total jobs.
- Repeat-clip rate: must be zero.
- Resources: peak RAM and GPU time if applicable.
- Concurrency: jobs completed under a measured load test.

## 13. Suggested 10-day implementation order

| Day | Deliverable |
|---|---|
| 1 | FastAPI skeleton, schemas, Docker Compose, health endpoints, mock adapters |
| 2 | Input upload/URL validation, storage, job model, idempotency, Celery status |
| 3 | ASR integration and word-level transcript contract; TTS for script input |
| 4 | Scene segmentation, Gemini structured output, deterministic coverage validator |
| 5 | Footage provider interface, one real provider if access/terms permit, mock provider |
| 6 | Embedding ranking, rights gate, duplicate prevention, fallback logic |
| 7 | FFmpeg normalisation/rendering, SRT, multiple aspect ratios |
| 8 | Timeline/manifest, QA checks, provider failure handling, logging |
| 9 | Ten-input test pack, hard cases, batch runner, API docs |
| 10 | Live deployment, measured metrics, report, demo recording, submission form evidence |

Build and verify all must-haves before investing heavily in Outstanding features. The schedule is a proposed execution plan; adjust based on actual provider access and render performance.
