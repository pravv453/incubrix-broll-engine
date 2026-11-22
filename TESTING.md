# Manual testing guide

Run every command from the repo root (`cd ~/incubrix-broll-engine`).
Commands assume `jq` is installed. If not, replace `| jq` with `| python3 -m json.tool`.

## 0. One-time shell setup (this Mac)

Docker, ffmpeg and uv were installed in user space, so put them on your PATH in every new terminal:

```bash
export PATH="$HOME/.local/bin:$HOME/Library/Python/3.9/bin:$PATH"
colima status || colima start --vm-type vz --cpu 4 --memory 8 --disk 40   # the Docker VM
docker info --format '{{.ServerVersion}}'                                   # should print a version
```

## 1. Start the stack and check health (Integration #1, #8)

```bash
cp -n .env.example .env                    # keyless defaults; add real keys later (section 9)
docker compose up --build -d
docker compose ps                          # api/objectstore/postgres/redis healthy, worker up
curl -s localhost:8000/health | jq
curl -s localhost:8000/ready  | jq         # database/queue/storage = ok
open http://localhost:8000                 # demo page
open http://localhost:8000/docs            # interactive API docs
```

Readiness failure test:
```bash
docker compose stop redis && curl -s -w '\nHTTP %{http_code}\n' localhost:8000/ready ; docker compose start redis
```
Expected: `503` with `SERVICE_NOT_READY` and `"queue": "error: ..."`.

## 2. Automated tests: one command, no keys (Integration #1)

```bash
docker compose run --rm tests                                  # unit + integration + e2e inside the image
# faster local alternatives (need uv + ffmpeg on PATH):
uv run pytest tests/unit -q
uv run pytest tests/integration -q
uv run pytest -m e2e -q
```

## 3. Must-have 1: both inputs work

A helper for this shell session that waits for a job and prints the result:
```bash
wait_job() { while :; do r=$(curl -s localhost:8000/api/v1/jobs/$1); s=$(echo "$r" | jq -r .status);
  echo "$(echo "$r" | jq -r '"\(.status) \(.stage) \(.progress)%"')"; [[ $s == succeeded || $s == failed || $s == cancelled ]] && break; sleep 3; done;
  echo "$r" | jq '{status, error, warnings: [.warnings[].code], result}'; }
```

**Text → narration → video**
```bash
J=$(curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H "Idempotency-Key: t-$(date +%s)" \
  -d '{"script":"Cloud computing lets a small team rent servers by the hour. Sales grew 40% in 2025. Trust is earned slowly.","aspect_ratios":["16:9","9:16","1:1"],"avoid":["alcohol"],"captions":{"burn_in":true,"srt":true}}' | jq -r .job_id); echo $J
wait_job $J
```
(With `TTS_PROVIDER=mock` the narration is a placeholder tone and the job carries a `MOCK_NARRATION` warning. Set Gemini TTS in section 9 for real speech.)

**Audio upload (MP3 / WAV / M4A)**. Make a real voiceover with macOS `say`:
```bash
say -o /tmp/vo.aiff "Cloud computing lets a small team rent powerful servers by the hour. Sales grew forty percent last year. Trust is earned slowly, one customer at a time. Every evening, the family shares a quiet meal at home."
ffmpeg -v error -y -i /tmp/vo.aiff /tmp/vo.mp3 && ffmpeg -v error -y -i /tmp/vo.aiff /tmp/vo.wav && ffmpeg -v error -y -i /tmp/vo.aiff -c:a aac /tmp/vo.m4a
for f in mp3 wav m4a; do
  J=$(curl -s -X POST localhost:8000/api/v1/broll/jobs -H "Idempotency-Key: up-$f-$(date +%s)" \
      -F audio_file=@/tmp/vo.$f -F 'options={"aspect_ratios":["16:9","9:16"]}' | jq -r .job_id); wait_job $J; done
```

**Audio URL** (any public MP3/WAV/M4A link):
```bash
J=$(curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H "Idempotency-Key: url-$(date +%s)" \
  -d '{"audio_url":"https://<public-host>/voiceover.mp3","aspect_ratios":["16:9"]}' | jq -r .job_id); wait_job $J
```

**IncuBrix scene list**
```bash
J=$(curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H "Idempotency-Key: sc-$(date +%s)" \
  -d '{"scenes":[{"text":"Our app keeps your week organised.","visual_hint":"phone calendar app"},{"text":"Teams plan projects together.","visual_hint":"team meeting laptops"}],"aspect_ratios":["9:16"]}' | jq -r .job_id); wait_job $J
```

## 4. Inspect the outputs (must-haves 2, 3, 4, 5)

```bash
curl -s localhost:8000/api/v1/jobs/$J/timeline | jq '.scenes[] | {scene_id, start_s, end_s, text, clip: .clip.asset_id, provider: .clip.provider, ai: .clip.is_ai_generated, licence: .clip.licence.name, score: .selection.score, rule: .selection.rule}'
# coverage: first start 0, last end == total_frames, contiguous
curl -s localhost:8000/api/v1/jobs/$J/timeline | jq '{total_frames, first: .scenes[0].start_frame, last: .scenes[-1].end_frame, gapless: ([.scenes as $s | range(1; $s|length) | $s[.].start_frame == $s[.-1].end_frame] | all)}'
# download everything
mkdir -p /tmp/out && curl -s localhost:8000/api/v1/jobs/$J | jq -r '.result | (.videos | to_entries[] | "\(.value) video_\(.key|sub(":";"x")).mp4"), "\(.captions_srt) captions.srt", "\(.manifest_json) manifest.json", "\(.timeline_json) timeline.json"' \
  | while read url name; do curl -s -o "/tmp/out/$name" "$url"; done; ls -la /tmp/out
# technical checks on each video (codec, size, fps, pix_fmt, durations)
for v in /tmp/out/video_*.mp4; do ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,pix_fmt,duration -of compact $v; done
# black / frozen frame scans (no output lines = none found)
for v in /tmp/out/video_*.mp4; do ffmpeg -hide_banner -i $v -vf "blackdetect=d=0.25,crop=iw:ih*0.72:0:0,freezedetect=n=-60dB:d=1" -an -f null - 2>&1 | grep -E "black_start|freeze_start"; done
# the service's own QA verdicts + rights summary
jq '.validation.outputs | map_values({passed, measured: {fps: .measured.fps, frames: .measured.frames, av_drift_s: .measured.av_drift_s}})' /tmp/out/manifest.json
jq '.rights | {all_commercial_use_allowed, ai_generated_scenes, assets: [.assets[] | {scene_id, provider, asset_id, licence: .licence.name}]}' /tmp/out/manifest.json
jq '.validation.repeat_clip_count' /tmp/out/manifest.json    # must be 0
open /tmp/out/video_9x16.mp4
```

## 5. Must-have 6: hard cases (named people/brands, double meanings, sad news, abstract, statistics)

```bash
python scripts/batch_runner.py --input tests/scenarios/hard --api http://localhost:8000 --output test-results/hard
jq '.rows[] | {scenario, status, error_code, warnings}' test-results/hard/summary.json
# look at what the planner decided per scene:
jq '.scenes[] | {text, concept: .visual_brief.visual_concept, entities: .visual_brief.named_entities, sensitivity: .visual_brief.sensitivity, warnings: [.warnings[].code], clip: .clip.asset_id, ai: .clip.is_ai_generated}' test-results/hard/h01_named_person_brand/timeline.json
jq '.scenes[] | {text, concept: .visual_brief.visual_concept, clip: .clip.asset_id}' test-results/hard/h02_double_meanings/timeline.json
jq '.scenes[] | {text, sensitivity: .visual_brief.sensitivity, clip: .clip.asset_id}' test-results/hard/h03_sad_news/timeline.json
jq '.scenes[] | .render["9:16"].crop' test-results/hard/h04_portrait_edge_subject/timeline.json      # saliency_crop / blur_pad
```

Audio hard cases (silent, music-only, wrong type, > 10 min, video renamed to .mp3):
```bash
python scripts/make_hard_fixtures.py
python scripts/batch_runner.py --input tests/scenarios/audio_hard --api http://localhost:8000 --output test-results/audio_hard
jq '.rows[] | {scenario, status, error_code}' test-results/audio_hard/summary.json
```
Expected: silent and music-only fail with `NO_SPEECH_DETECTED`. Wrong type and the renamed video are rejected with `415 UNSUPPORTED_AUDIO_FORMAT`. Over 10 min is rejected with `AUDIO_TOO_LONG`.
Long narration: `python scripts/batch_runner.py --input tests/scenarios/long --api http://localhost:8000 --output test-results/long`.

## 6. Must-have 7: resilience ("switch a source off during a job")

```bash
J=$(curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H "Idempotency-Key: down-$(date +%s)" \
  -d '{"script":"Bright offices are full of focused work. Teams meet to plan the week. The city skyline glows at dawn.","aspect_ratios":["16:9"]}' | jq -r .job_id)
docker compose exec worker sh -c 'echo "{\"mock\":\"down\"}" > /shared/faults.json'      # use "pixabay" when Pixabay is enabled
wait_job $J                                                                             # finishes on the generated source
docker compose exec worker rm -f /shared/faults.json
curl -s localhost:8000/api/v1/jobs/$J/timeline | jq '.scenes[] | {scene_id, provider: .clip.provider, rule: .selection.rule}'
```
Other fault modes: `empty` (no results), `corrupt` (garbage downloads → next candidate), `slow` (timeout).
Everything down (`{"mock":"down","generated":"down"}`) → job fails with `FOOTAGE_PROVIDER_UNAVAILABLE`, `retryable: true`.

Two users at once:
```bash
for i in 1 2; do curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H "Idempotency-Key: two-$i-$(date +%s)" \
  -d "{\"script\":\"User $i says the ocean waves roll onto the beach.\"}" | jq -r .job_id & done; wait
```

## 7. Integration Standard checks

```bash
# 2. OpenAPI 3.1 matches the live app
curl -s localhost:8000/openapi.json | python3 -c "import sys,json,yaml; live=json.load(sys.stdin); print('MATCH' if live==yaml.safe_load(open('openapi.yaml')) else 'DRIFT', live['openapi'])"
# 4. Idempotency: same key + same body -> same job (Idempotent-Replayed: true); different body -> 409
B='{"script":"Idempotency demo sentence."}'
curl -s -i -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H 'Idempotency-Key: idem-demo' -d "$B" | grep -iE "^HTTP|replayed|job_id"
curl -s -i -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H 'Idempotency-Key: idem-demo' -d "$B" | grep -iE "^HTTP|replayed|job_id"
curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H 'Idempotency-Key: idem-demo' -d '{"script":"different"}' | jq
# 5. Standard errors
curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -d '{"script":"x"}' | jq          # MISSING_IDEMPOTENCY_KEY
curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H 'Idempotency-Key: e1' -d '{"script":"  "}' | jq     # EMPTY_SCRIPT
curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H 'Idempotency-Key: e2' -d '{"audio_url":"http://169.254.169.254/x.mp3"}' | jq  # UNSAFE_URL
curl -s localhost:8000/api/v1/jobs/broll_nope | jq                                                                                          # JOB_NOT_FOUND
# 8. Structured logs with job_id and correlation id
curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H 'Idempotency-Key: log-1' -H 'X-Correlation-Id: my-trace-123' -d '{"script":"Logs carry the correlation id."}' | jq -r .job_id
docker compose logs api worker --no-log-prefix | grep my-trace-123 | head -5
# 9. Manifest: models/providers/versions, settings, seed, timings, cost, warnings, rights
jq '{providers: (.providers | keys), seed, timings_s, cost: .cost_estimate.total_known_usd, unknown_cost: .cost_estimate.unknown_items, warnings: [.warnings[].code]}' /tmp/out/manifest.json
# cancel
J=$(curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H "Idempotency-Key: cx-$(date +%s)" -d '{"script":"Cancel this job please. It has a second sentence."}' | jq -r .job_id)
curl -s -X POST localhost:8000/api/v1/jobs/$J/cancel -H 'Idempotency-Key: cx' | jq '{status, stage}'
```

Callback with HMAC: use a public request inspector (e.g. a webhook.site URL). Private and local addresses are blocked by the SSRF guard on purpose.
```bash
echo 'CALLBACK_HMAC_SECRET=my-secret' >> .env && docker compose up -d
curl -s -X POST localhost:8000/api/v1/broll/jobs -H 'Content-Type: application/json' -H "Idempotency-Key: cb-$(date +%s)" \
  -d '{"script":"Callback test.","callback_url":"https://webhook.site/<your-id>"}' | jq
# verify the received body (saved as body.json) against the X-IncuBrix-Signature header:
python3 -c "import hmac,hashlib,sys; print(hmac.new(b'my-secret', open('body.json','rb').read(), hashlib.sha256).hexdigest())"
```

## 8. Advanced: clip swap (partial re-render)

```bash
SW=$(curl -s -X POST localhost:8000/api/v1/jobs/$J/scenes/scene_002/clip -H 'Content-Type: application/json' -H "Idempotency-Key: sw-$(date +%s)" \
  -d '{"reason":"want a different shot"}' | jq -r .job_id); wait_job $SW
# variants: {"candidate_rank":2}  {"query":"aerial city at night"}  {"provider":"mock","asset_id":"ocean"}
curl -s localhost:8000/api/v1/jobs/$SW/timeline | jq '.history[-1]'      # rerender_s vs parent_full_render_s, segments reused
```

## 9. Real providers (Gemini + Pixabay)

Edit `.env`:
```dotenv
GEMINI_API_KEY=...
TTS_PROVIDER=gemini
GEMINI_TTS_MODEL=<current TTS model id>
LLM_PROVIDER=gemini
GEMINI_TEXT_MODEL=<current Flash model id>
GENERATED_PROVIDER=gemini
GEMINI_IMAGE_MODEL=<current image model id>
JUDGE_PROVIDER=gemini
GEMINI_JUDGE_MODEL=<current multimodal model id>
FOOTAGE_PROVIDERS=pixabay
PIXABAY_API_KEY=...
```
Then `docker compose up -d` and run the scenario packs:
```bash
python scripts/batch_runner.py --input tests/scenarios/core --api http://localhost:8000 --output test-results/core --sample-resources
python scripts/batch_runner.py --input tests/scenarios/multilingual --api http://localhost:8000 --output test-results/ml
# rate every scene 1-5 in test-results/core/ratings_template.csv (column human_rating_1_5), then:
python scripts/eval_relevance.py test-results/core/ratings_template.csv --out test-results/core/relevance.json
```

## 10. Production numbers (Metrics tab)

```bash
# warm turnaround for ~60 s of narration (06_saving_money_60s), run twice: first = cold, second = warm
python scripts/batch_runner.py --input tests/scenarios/core/06_saving_money_60s.json --api http://localhost:8000 --output test-results/turnaround_cold
python scripts/batch_runner.py --input tests/scenarios/core/06_saving_money_60s.json --api http://localhost:8000 --output test-results/turnaround_warm
# concurrency: 4 jobs at once with MAX_CONCURRENT_JOBS=2, sampling docker stats (peak memory)
python scripts/batch_runner.py --input tests/scenarios/core --api http://localhost:8000 --output test-results/concurrency --concurrency 4 --sample-resources
jq '{wall_clock_s, succeeded, total_narration_s}' test-results/concurrency/summary.json
sort -t, -k4 -h test-results/concurrency/resources.csv | grep worker | tail -3
```

## 11. Stop / reset

```bash
docker compose down            # keep data
docker compose down -v         # wipe DB, storage and model cache
colima stop                    # stop the VM
```
