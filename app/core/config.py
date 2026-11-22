"""All runtime settings come from environment variables (Integration Standard #6).

Every field here must also appear in `.env.example`; `tests/unit/test_core.py`
enforces that so the two cannot drift.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- app ---
    app_env: str = "development"
    app_version: str = "0.1.0"
    git_sha: str = "unknown"
    log_level: str = "INFO"
    demo_access_token: str = ""  # empty = auth disabled (local dev / tests)
    rate_limit_per_minute: int = 30  # POSTs per client IP per minute; 0 disables
    public_base_url: str = "http://localhost:8000"

    # --- infrastructure ---
    database_url: str = "postgresql+psycopg://app:app@postgres:5432/broll"
    redis_url: str = "redis://redis:6379/0"
    job_runner: str = "celery"  # celery | inline (tests: run the job in-process)
    storage_backend: str = "s3"  # s3 | local
    local_storage_dir: str = "/tmp/broll-storage"
    s3_endpoint: str = "http://objectstore:9000"
    s3_public_endpoint: str = ""  # host used in presigned links; defaults to s3_endpoint
    s3_access_key: str = "broll"
    s3_secret_key: str = "change-me-s3-secret"
    s3_bucket: str = "broll"
    s3_region: str = "us-east-1"
    s3_presign_expiry_s: int = 86400
    work_dir: str = "/tmp/broll-work"
    model_cache_dir: str = "/tmp/broll-models"

    # --- providers (adapter selection) ---
    asr_provider: str = "mock"  # mock | faster_whisper
    asr_model: str = "small"
    asr_compute_type: str = "int8"
    tts_provider: str = "mock"  # mock | gemini
    llm_provider: str = "rules"  # rules | gemini
    embedding_provider: str = "mock"  # mock | fastembed
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    vision_provider: str = "disabled"  # disabled | mock | clip
    vision_text_model: str = "Qdrant/clip-ViT-B-32-text"
    vision_image_model: str = "Qdrant/clip-ViT-B-32-vision"
    face_detector: str = "disabled"  # disabled | yunet
    face_model_path: str = "/opt/models/face_detection_yunet_2023mar.onnx"
    judge_provider: str = "disabled"  # disabled | mock | gemini  (independent relevance judge)
    footage_providers: str = "mock"  # comma-separated stock providers: mock | pixabay
    generated_provider: str = "mock"  # disabled | mock | gemini  (AI image -> motion clip)
    gemini_api_key: str = ""
    gemini_text_model: str = ""  # set from the live Gemini model catalogue
    gemini_tts_model: str = ""
    gemini_image_model: str = ""
    gemini_judge_model: str = ""
    gemini_tts_voice_map: str = "en-IN-female=Kore,en-IN-male=Puck,hi-IN-female=Kore,ta-IN-female=Kore"
    pixabay_api_key: str = ""
    footage_max_candidates: int = 12
    footage_request_timeout_s: float = 20.0
    footage_cache_ttl_s: int = 86400
    fault_injection_file: str = ""  # JSON {"provider": "down|empty|corrupt|slow"}; read at call time

    # --- ranking & selection ---
    rank_w_semantic: float = 0.45
    rank_w_visual: float = 0.20
    rank_w_duration: float = 0.10
    rank_w_aspect: float = 0.10
    rank_w_quality: float = 0.10
    rank_w_rights: float = 0.05
    stock_min_score: float = 0.55  # tau: below this, a generated clip is preferred (when allowed)
    candidates_to_probe: int = 4  # top-N downloaded & QA'd per scene before giving up

    # --- output / media ---
    output_fps: int = 30
    output_width_landscape: int = 1920
    output_height_landscape: int = 1080
    output_width_portrait: int = 1080
    output_height_portrait: int = 1920
    output_width_square: int = 1080
    output_height_square: int = 1080
    video_crf: int = 21
    video_preset: str = "veryfast"
    audio_bitrate: str = "192k"
    colour_normalise: bool = True
    target_luma: float = 0.45  # mean luma (0-1) every clip is nudged toward
    caption_font: str = "Noto Sans"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    ffmpeg_threads: int = 0

    # --- scenes & captions ---
    min_scene_s: float = 1.5
    max_scene_s: float = 8.0
    target_scene_s: float = 3.0
    caption_max_chars: int = 42
    caption_max_words: int = 8

    # --- limits ---
    max_audio_duration_s: float = 600.0
    max_upload_mb: int = 200
    max_script_chars: int = 10000
    max_concurrent_jobs: int = 2
    url_fetch_timeout_s: float = 30.0
    min_speech_ratio: float = 0.15  # below this share of speech -> NO_SPEECH_DETECTED

    # --- callbacks ---
    callback_hmac_secret: str = ""
    callback_allowed_hosts: str = ""  # comma-separated; empty = any public host
    callback_timeout_s: float = 10.0

    # --- cost model (USD; blank = unknown, reported as such in the manifest) ---
    cost_tts_per_1k_chars: str = ""
    cost_llm_per_call: str = ""
    cost_image_per_generation: str = ""
    cost_judge_per_call: str = ""
    cost_compute_per_hour: str = ""

    seed: int = 1234

    @property
    def footage_provider_list(self) -> list[str]:
        return [p.strip() for p in self.footage_providers.split(",") if p.strip()]

    @property
    def voice_map(self) -> dict[str, str]:
        out = {}
        for pair in self.gemini_tts_voice_map.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def cost(self, name: str) -> float | None:
        raw = getattr(self, name, "")
        try:
            return float(raw) if str(raw).strip() else None
        except ValueError:
            return None

    def public_dict(self) -> dict:
        """Settings snapshot for the manifest, with secrets removed."""
        secret_words = ("key", "secret", "token", "password")
        return {
            k: ("[set]" if v else "") if any(w in k for w in secret_words) else v
            for k, v in self.model_dump().items()
            if k not in ("database_url",)
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
