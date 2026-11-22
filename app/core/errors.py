"""Standard error envelope (Integration Standard #5).

Every error leaving the API has the shape
    {"error": {"code": "UPPER_SNAKE_CASE", "message": str, "retryable": bool, "details": {}}}
The code catalogue below is the single source of truth; the README table is
generated from it (see scripts/gen_error_table.py) so docs cannot drift.
"""
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorCode(str, Enum):
    # code = (http_status, retryable, description)
    INVALID_REQUEST = "INVALID_REQUEST"
    MISSING_INPUT = "MISSING_INPUT"
    MULTIPLE_INPUTS = "MULTIPLE_INPUTS"
    MISSING_IDEMPOTENCY_KEY = "MISSING_IDEMPOTENCY_KEY"
    UNSUPPORTED_AUDIO_FORMAT = "UNSUPPORTED_AUDIO_FORMAT"
    AUDIO_TOO_LONG = "AUDIO_TOO_LONG"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    EMPTY_SCRIPT = "EMPTY_SCRIPT"
    SCRIPT_TOO_LONG = "SCRIPT_TOO_LONG"
    INVALID_ASPECT_RATIO = "INVALID_ASPECT_RATIO"
    UNSAFE_URL = "UNSAFE_URL"
    INPUT_DOWNLOAD_FAILED = "INPUT_DOWNLOAD_FAILED"
    NO_SPEECH_DETECTED = "NO_SPEECH_DETECTED"
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
    TTS_FAILED = "TTS_FAILED"
    SCENE_PLANNING_FAILED = "SCENE_PLANNING_FAILED"
    NO_ELIGIBLE_FOOTAGE = "NO_ELIGIBLE_FOOTAGE"
    FOOTAGE_PROVIDER_UNAVAILABLE = "FOOTAGE_PROVIDER_UNAVAILABLE"
    FOOTAGE_DOWNLOAD_FAILED = "FOOTAGE_DOWNLOAD_FAILED"
    RIGHTS_CHECK_FAILED = "RIGHTS_CHECK_FAILED"
    RENDER_FAILED = "RENDER_FAILED"
    OUTPUT_VALIDATION_FAILED = "OUTPUT_VALIDATION_FAILED"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_FOUND = "NOT_FOUND"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    SCENE_NOT_FOUND = "SCENE_NOT_FOUND"
    JOB_NOT_READY = "JOB_NOT_READY"
    JOB_ALREADY_COMPLETED = "JOB_ALREADY_COMPLETED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    SERVICE_NOT_READY = "SERVICE_NOT_READY"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ERROR_CATALOGUE: dict[ErrorCode, tuple[int, bool, str]] = {
    ErrorCode.INVALID_REQUEST: (422, False, "Request body or parameters failed validation."),
    ErrorCode.MISSING_INPUT: (422, False, "None of audio_file, audio_url, script or scenes was supplied."),
    ErrorCode.MULTIPLE_INPUTS: (422, False, "More than one primary input was supplied; send exactly one."),
    ErrorCode.MISSING_IDEMPOTENCY_KEY: (400, False, "POST requests require an Idempotency-Key header."),
    ErrorCode.UNSUPPORTED_AUDIO_FORMAT: (415, False, "Audio is not a decodable MP3, WAV or M4A file."),
    ErrorCode.AUDIO_TOO_LONG: (422, False, "Audio exceeds the maximum duration (default 600 s)."),
    ErrorCode.FILE_TOO_LARGE: (413, False, "Upload or download exceeds the configured size limit."),
    ErrorCode.EMPTY_SCRIPT: (422, False, "Script (or every scene text) is empty after trimming."),
    ErrorCode.SCRIPT_TOO_LONG: (422, False, "Script exceeds the configured character limit."),
    ErrorCode.INVALID_ASPECT_RATIO: (422, False, "Aspect ratio must be one of 16:9, 9:16, 1:1."),
    ErrorCode.UNSAFE_URL: (422, False, "URL scheme/host is not allowed (private, loopback, metadata, non-HTTP)."),
    ErrorCode.INPUT_DOWNLOAD_FAILED: (502, True, "The input audio URL could not be fetched."),
    ErrorCode.NO_SPEECH_DETECTED: (422, False, "Audio is silent or contains no intelligible speech (e.g. music only)."),
    ErrorCode.TRANSCRIPTION_FAILED: (502, True, "Speech recognition failed."),
    ErrorCode.TTS_FAILED: (502, True, "Narration generation failed."),
    ErrorCode.SCENE_PLANNING_FAILED: (502, True, "Scene planning failed and no deterministic fallback was possible."),
    ErrorCode.NO_ELIGIBLE_FOOTAGE: (422, False, "No rights-eligible, safe, unused clip exists for a scene."),
    ErrorCode.FOOTAGE_PROVIDER_UNAVAILABLE: (503, True, "All configured footage providers failed."),
    ErrorCode.FOOTAGE_DOWNLOAD_FAILED: (502, True, "Selected clips and all fallbacks failed to download/decode."),
    ErrorCode.RIGHTS_CHECK_FAILED: (422, False, "A clip failed the rights gate (e.g. on a manual replacement)."),
    ErrorCode.RENDER_FAILED: (500, True, "FFmpeg rendering failed."),
    ErrorCode.OUTPUT_VALIDATION_FAILED: (500, False, "Rendered output failed QA (duration, fps, black/frozen frames)."),
    ErrorCode.UNAUTHORIZED: (401, False, "Missing or invalid access token."),
    ErrorCode.RATE_LIMITED: (429, True, "Too many requests; retry after the indicated delay."),
    ErrorCode.NOT_FOUND: (404, False, "Route not found."),
    ErrorCode.JOB_NOT_FOUND: (404, False, "No job with this id."),
    ErrorCode.SCENE_NOT_FOUND: (404, False, "No scene with this id in the job's timeline."),
    ErrorCode.JOB_NOT_READY: (409, True, "Job has not finished; the requested artefact is not available yet."),
    ErrorCode.JOB_ALREADY_COMPLETED: (409, False, "Job is already in a terminal state."),
    ErrorCode.IDEMPOTENCY_CONFLICT: (409, False, "Idempotency-Key was reused with a different request body."),
    ErrorCode.STORAGE_UNAVAILABLE: (503, True, "Object storage is unreachable."),
    ErrorCode.SERVICE_NOT_READY: (503, True, "A critical dependency (DB, queue, storage) is unavailable."),
    ErrorCode.INTERNAL_ERROR: (500, True, "Unexpected server error."),
}


class AppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
    ) -> None:
        status, default_retryable, default_msg = ERROR_CATALOGUE[code]
        self.code = code
        self.message = message or default_msg
        self.details = details or {}
        self.retryable = default_retryable if retryable is None else retryable
        self.http_status = http_status or status
        super().__init__(f"{code.value}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            }
        }


def error_response(err: AppError, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=err.http_status, content=err.to_dict(), headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return error_response(AppError(ErrorCode.INVALID_REQUEST, details={"errors": errors}))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return error_response(AppError(ErrorCode.NOT_FOUND))
        return error_response(
            AppError(ErrorCode.INVALID_REQUEST, message=str(exc.detail), http_status=exc.status_code)
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("app").exception("unhandled_error")
        return error_response(AppError(ErrorCode.INTERNAL_ERROR))
