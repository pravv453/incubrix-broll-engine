"""POST /api/v1/broll/jobs: accepts JSON (audio_url | script | scenes) or
multipart (audio_file + options). Validation that takes < 2 s (schema, URL
safety, ffprobe of an upload) happens inline; everything else is a job."""
import asyncio
import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import correlation_id_var
from app.db import repositories as repo
from app.schemas.jobs import ErrorEnvelope, JobCreated
from app.schemas.requests import BrollJobOptions, BrollJobRequest
from app.services.input_service import validate_audio_file
from app.storage.s3_storage import get_storage, sha256_file
from app.utils.urls import check_public_url
from app.workers.tasks import enqueue

router = APIRouter(prefix="/api/v1", tags=["broll"])

ERRORS = {c: {"model": ErrorEnvelope} for c in (400, 401, 409, 413, 415, 422, 429, 503)}

REQUEST_BODY = {
    "required": True,
    "content": {
        "application/json": {"schema": {"$ref": "#/components/schemas/BrollJobRequest"}},
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["audio_file"],
                "properties": {
                    "audio_file": {"type": "string", "format": "binary", "description": "MP3, WAV or M4A, up to 10 minutes"},
                    "options": {"type": "string", "description": "JSON-encoded BrollJobOptions (everything except audio_url/script/scenes)"},
                },
            }
        },
    },
}


def require_idempotency_key(key: str | None) -> str:
    if not key or not key.strip():
        raise AppError(ErrorCode.MISSING_IDEMPOTENCY_KEY)
    if len(key) > 255:
        raise AppError(ErrorCode.INVALID_REQUEST, message="Idempotency-Key longer than 255 characters")
    return key.strip()


def _validation_error(exc: ValidationError) -> AppError:
    return AppError(ErrorCode.INVALID_REQUEST, details={"errors": [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]})


def created_response(job, created: bool) -> JSONResponse:
    body = JobCreated(job_id=job.id, status=job.status, progress=job.progress,
                      status_url=f"/api/v1/jobs/{job.id}", created_at=job.created_at)
    return JSONResponse(status_code=202, content=body.model_dump(mode="json"),
                        headers={"Idempotent-Replayed": "false" if created else "true", "Location": body.status_url})


async def _store_upload(upload, s) -> dict:
    limit = s.max_upload_mb * 1024 * 1024
    tmpdir = Path(tempfile.mkdtemp(prefix="upload-"))
    name = Path(upload.filename or "audio").name
    path = tmpdir / ("upload" + Path(name).suffix.lower())
    size = 0
    with open(path, "wb") as f:
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > limit:
                raise AppError(ErrorCode.FILE_TOO_LARGE, details={"limit_mb": s.max_upload_mb})
            f.write(chunk)
    info = await asyncio.to_thread(validate_audio_file, path, name)
    sha = await asyncio.to_thread(sha256_file, path)
    key = f"uploads/{sha}{path.suffix}"
    storage = get_storage()
    if not await asyncio.to_thread(storage.exists, key):
        await asyncio.to_thread(storage.put_file, key, path)
    path.unlink(missing_ok=True)
    return {"key": key, "sha256": sha, "filename": name, "size_bytes": size, "probe": info}


@router.post(
    "/broll/jobs", status_code=202, response_model=JobCreated, responses=ERRORS,
    summary="Create a B-roll job from audio (upload or URL), a script, or an IncuBrix scene list",
    openapi_extra={"requestBody": REQUEST_BODY},
)
async def create_broll_job(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key",
                                         description="Required. Same key + same body returns the same job."),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id"),
) -> JSONResponse:
    s = get_settings()
    key = require_idempotency_key(idempotency_key)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        cl = int(request.headers.get("content-length") or 0)
        if cl > s.max_upload_mb * 1024 * 1024 + 65536:
            raise AppError(ErrorCode.FILE_TOO_LARGE, details={"limit_mb": s.max_upload_mb})
        form = await request.form()
        upload = form.get("audio_file")
        if upload is None or isinstance(upload, str):
            if any(k in form for k in ("script", "audio_url", "scenes")):
                raise AppError(ErrorCode.INVALID_REQUEST, message="multipart accepts audio_file + options only; send text/URL inputs as JSON")
            raise AppError(ErrorCode.MISSING_INPUT, message="multipart requests need an audio_file part")
        try:
            raw = form.get("options") or "{}"
            opts = BrollJobOptions.model_validate(json.loads(raw))
        except json.JSONDecodeError:
            raise AppError(ErrorCode.INVALID_REQUEST, message="options must be a JSON object") from None
        except ValidationError as exc:
            raise _validation_error(exc) from None
        if opts.callback_url:
            check_public_url(str(opts.callback_url))
        stored = await _store_upload(upload, s)
        request_json = {**opts.model_dump(mode="json"), "audio_url": None, "script": None, "scenes": None,
                        "audio_file": stored}
    else:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            raise AppError(ErrorCode.INVALID_REQUEST, message="body must be JSON or multipart/form-data") from None
        if not isinstance(body, dict):
            raise AppError(ErrorCode.INVALID_REQUEST, message="body must be a JSON object")
        try:
            req = BrollJobRequest.model_validate(body)
        except ValidationError as exc:
            raise _validation_error(exc) from None
        if req.script is not None and len(req.script) > s.max_script_chars:
            raise AppError(ErrorCode.SCRIPT_TOO_LONG, details={"chars": len(req.script), "max_chars": s.max_script_chars})
        if req.audio_url is not None:
            await asyncio.to_thread(check_public_url, str(req.audio_url))
        if req.callback_url is not None:
            await asyncio.to_thread(check_public_url, str(req.callback_url))
        request_json = req.model_dump(mode="json")
    job, created = await asyncio.to_thread(
        repo.create_or_get_job, idempotency_key=key, request_json=request_json,
        correlation_id=x_correlation_id or correlation_id_var.get())
    if created:
        enqueue(job.id)
    return created_response(job, created)
