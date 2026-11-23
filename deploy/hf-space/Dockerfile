# Hugging Face Space (free CPU: 2 vCPU / 16 GB) - single-container demo.
# The Space repo holds only this file + README.md. The code is cloned at build time from the
# PRIVATE GitHub repo with a read-only deploy key passed as the build secret GITHUB_DEPLOY_KEY.
# Single-container mode: in-process job runner (MAX_CONCURRENT_JOBS slots), SQLite, local file storage.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-noto-core curl ca-certificates libgl1 libglib2.0-0 git openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ARG REPO=git@github.com:pravv453/incubrix-broll-engine.git
ARG REF=main
# GitHub's published SSH host key (pinned, no trust-on-first-use)
RUN mkdir -p /root/.ssh && echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" > /root/.ssh/known_hosts
RUN --mount=type=secret,id=GITHUB_DEPLOY_KEY,mode=0444,required=true \
    install -m 600 /run/secrets/GITHUB_DEPLOY_KEY /tmp/deploy_key \
    && GIT_SSH_COMMAND="ssh -i /tmp/deploy_key -o IdentitiesOnly=yes" git clone --depth 1 --branch "$REF" "$REPO" /app \
    && rm -f /tmp/deploy_key && echo "GIT_SHA=$(git -C /app rev-parse --short HEAD)" > /app/.build_sha \
    && rm -rf /app/.git

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models/hf MODEL_CACHE_DIR=/models
RUN uv sync --frozen --no-install-project --group ml

RUN mkdir -p /opt/models && curl -fsSL -o /opt/models/face_detection_yunet_2023mar.onnx \
      https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    && test "$(stat -c %s /opt/models/face_detection_yunet_2023mar.onnx)" -gt 100000

# Bake the local models into the image so the first job doesn't download them (shorter cold start).
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8', download_root='/models')" \
 && python -c "from fastembed import TextEmbedding, ImageEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/models'); TextEmbedding('Qdrant/clip-ViT-B-32-text', cache_dir='/models'); ImageEmbedding('Qdrant/clip-ViT-B-32-vision', cache_dir='/models')"

# Spaces run the container as uid 1000
RUN useradd --create-home --uid 1000 app && mkdir -p /tmp/broll-work /tmp/broll-storage \
    && chown -R app /tmp/broll-work /tmp/broll-storage /models /app
USER app

ENV JOB_RUNNER=inline STORAGE_BACKEND=local DATABASE_URL=sqlite:////tmp/broll.db \
    LOCAL_STORAGE_DIR=/tmp/broll-storage WORK_DIR=/tmp/broll-work FORWARDED_ALLOW_IPS=*
EXPOSE 8000
CMD ["sh", "-c", ". /app/.build_sha && export GIT_SHA && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
