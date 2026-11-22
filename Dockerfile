# One image for api, worker and tests.
FROM python:3.12-slim-bookworm

# ffmpeg/ffprobe own the render path (libass for captions). Noto fonts so burned-in
# Hindi (Devanagari) and Tamil captions render real glyphs instead of tofu boxes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-noto-core curl ca-certificates libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models/hf \
    MODEL_CACHE_DIR=/models

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --group ml

# OpenCV YuNet face detector (MIT licence, opencv_zoo), ~230 KB
RUN mkdir -p /opt/models && curl -fsSL -o /opt/models/face_detection_yunet_2023mar.onnx \
      https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
    && test "$(stat -c %s /opt/models/face_detection_yunet_2023mar.onnx)" -gt 100000

COPY . .

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /tmp/broll-work /models && chown -R app /tmp/broll-work /models /app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
