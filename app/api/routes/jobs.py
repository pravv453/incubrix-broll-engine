import asyncio
import json

from fastapi import APIRouter, Header
from fastapi.responses import FileResponse, JSONResponse

from app.api.routes.broll import ERRORS, created_response, require_idempotency_key
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import correlation_id_var
from app.db import repositories as repo
from app.db.models import Job
from app.schemas.jobs import JobCreated, JobStatus, SwapClipRequest
from app.storage.s3_storage import LocalStorage, get_storage
from app.workers.tasks import enqueue

router = APIRouter(prefix="/api/v1", tags=["jobs"])


def _links(result: dict | None) -> dict | None:
    if not result:
        return None
    st = get_storage()
    out = {"videos": {r: st.url(k) for r, k in (result.get("videos") or {}).items()}}
    for k in ("captions_srt", "timeline_json", "manifest_json"):
        if result.get(k):
            out[k] = st.url(result[k])
    return out


def job_status_payload(job: Job) -> dict:
    return JobStatus(
        job_id=job.id, kind=job.kind, parent_job_id=job.parent_job_id, status=job.status, stage=job.stage,
        progress=job.progress, created_at=job.created_at, updated_at=job.updated_at, started_at=job.started_at,
        finished_at=job.finished_at, warnings=job.warnings or [], error=job.error if job.status in ("failed", "cancelled") else None,
        result=_links(job.result) if job.status == "succeeded" else None,
        timeline_url=f"/api/v1/jobs/{job.id}/timeline" if job.status == "succeeded" else None,
    ).model_dump(mode="json")


@router.get("/jobs/{job_id}", response_model=JobStatus, responses={404: ERRORS[409]}, summary="Job status, progress and result links")
async def get_job(job_id: str) -> JSONResponse:
    job = await asyncio.to_thread(repo.require_job, job_id)
    return JSONResponse(job_status_payload(job))


@router.post("/jobs/{job_id}/cancel", status_code=202, response_model=JobStatus, responses={404: ERRORS[409], **ERRORS},
             summary="Cancel a queued or running job (stops at the next checkpoint)")
async def cancel_job(job_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    require_idempotency_key(idempotency_key)  # cancel is naturally idempotent: same key/body -> same job state
    job = await asyncio.to_thread(repo.require_job, job_id)
    if job.status in repo.TERMINAL:
        if job.status == "cancelled":
            return JSONResponse(status_code=202, content=job_status_payload(job))
        raise AppError(ErrorCode.JOB_ALREADY_COMPLETED, details={"status": job.status})
    if job.status == "queued":
        job = await asyncio.to_thread(repo.finish_job, job_id, "cancelled",
                                      error={"code": "JOB_CANCELLED", "message": "cancelled by user", "retryable": False, "details": {}})
        await asyncio.to_thread(repo.update_job, job_id, cancel_requested=True)
    else:
        job = await asyncio.to_thread(repo.update_job, job_id, cancel_requested=True)
    return JSONResponse(status_code=202, content=job_status_payload(repo.require_job(job_id)))


def _timeline(job: Job) -> dict:
    if job.status != "succeeded":
        raise AppError(ErrorCode.JOB_NOT_READY, details={"status": job.status})
    return json.loads(get_storage().get_bytes(job.result["timeline_json"]))


@router.get("/jobs/{job_id}/timeline", responses={404: ERRORS[409], 409: ERRORS[409]}, summary="The job's timeline.json")
async def get_timeline(job_id: str) -> JSONResponse:
    job = await asyncio.to_thread(repo.require_job, job_id)
    return JSONResponse(await asyncio.to_thread(_timeline, job))


@router.post("/jobs/{job_id}/scenes/{scene_id}/clip", status_code=202, response_model=JobCreated,
             responses={404: ERRORS[409], **ERRORS},
             summary="Replace one scene's clip; only that scene's segments are re-rendered (new job)")
async def swap_clip(job_id: str, scene_id: str, body: SwapClipRequest,
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id")) -> JSONResponse:
    key = require_idempotency_key(idempotency_key)
    parent = await asyncio.to_thread(repo.require_job, job_id)
    timeline = await asyncio.to_thread(_timeline, parent)
    if not any(sc["scene_id"] == scene_id for sc in timeline["scenes"]):
        raise AppError(ErrorCode.SCENE_NOT_FOUND, details={"scene_id": scene_id})
    req = {"scene_id": scene_id, **body.model_dump(mode="json")}
    job, created = await asyncio.to_thread(repo.create_or_get_job, idempotency_key=key, request_json=req, kind="swap",
                                           parent_job_id=job_id, correlation_id=x_correlation_id or correlation_id_var.get())
    if created:
        enqueue(job.id)
    return created_response(job, created)


@router.get("/files/{key:path}", include_in_schema=True, responses={404: ERRORS[409]},
            summary="Download an output (only with STORAGE_BACKEND=local; S3 mode returns presigned links instead)")
async def get_file(key: str):
    st = get_storage()
    if get_settings().storage_backend != "local" or not isinstance(st, LocalStorage):
        raise AppError(ErrorCode.NOT_FOUND)
    p = st.path(key)
    if not p.exists():
        raise AppError(ErrorCode.NOT_FOUND)
    return FileResponse(p)


_ = JobCreated  # re-exported for OpenAPI generation
