# AI use

## Coding assistant

| Tool | Model | Used for |
|---|---|---|
| Claude Code (VS Code extension, Anthropic) | Claude Opus 5.5 | Repository scaffolding; implementing the services, adapters, tests, batch runner, Docker/Compose files and docs from my design documents (`idea.md`, `pipeline.md`, `prompt.md`); running tests and debugging failures; setting up the local toolchain (uv, static ffmpeg, Colima/Docker) |

How it was used:
- I wrote the design docs and the task prompt. The assistant proposed improvements in `PROPOSALS.md`, and I reviewed and approved them before implementation. Examples: generated clips as the second source, motion in mock clips, per-clip QA, a face detector for `avoid`, saliency crop, the rules planner as LLM fallback/mock, and segment-based partial re-render.
- The assistant wrote code in small increments and ran the test suite after each step. Bugs found this way are recorded in `IMPLEMENTATION.md`, for example the `must_avoid` length crash on sad-news scripts and the static-crop freeze in 9:16.
- All design decisions, thresholds and licence choices are documented so that I can explain and change them. I am responsible for the code and can walk through every module.

## AI inside the product (runtime)

These are listed with licences in `SOURCES.md`, and every run records them in `manifest.json → providers`:
- ASR: faster-whisper (Whisper). TTS: Gemini TTS (optional).
- Scene planning: Gemini Flash structured output (optional) or the deterministic rules planner.
- Relevance: bge-small text embeddings and CLIP ViT-B/32 (selection). Gemini vision acts as an independent judge (optional, evaluation only).
- Safety: YuNet face detector.
- Generated footage: Gemini image model (optional). Every generated clip is flagged `is_ai_generated: true` in `timeline.json` and listed under `manifest.rights.ai_generated_scenes`.
