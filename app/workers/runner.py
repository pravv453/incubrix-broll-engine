"""Runs one job end to end and records the terminal state. Shared by the Celery
task and the inline (test) runner so both behave identically."""
import json
import logging
import time
import traceback

import httpx

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import correlation_id_var, job_id_var
from app.core.security import sign_body
from app.db import repositories as repo
from app.services.pipeline import JobCancelled, run_broll, run_swap
from app.utils.urls import check_public_url

log = logging.getLogger("app.worker")


def execute(job_id: str) -> None:
    job = repo.get_job(job_id)
    if job is None:
        log.error("job_missing", extra={"job_id": job_id})
        return
    t1, t2 = job_id_var.set(job_id), correlation_id_var.set(job.correlation_id)
    try:
        if job.status in repo.TERMINAL:
            log.info("job_already_terminal")
            return
        if job.cancel_requested:
            repo.finish_job(job_id, "cancelled", error=AppError(ErrorCode.JOB_ALREADY_COMPLETED, message="cancelled before start").to_dict()["error"])
            return
        repo.mark_running(job_id)
        log.info("job_started", extra={"kind": job.kind})
        t0 = time.perf_counter()
        try:
            result = run_swap(job_id) if job.kind == "swap" else run_broll(job_id)
            repo.finish_job(job_id, "succeeded", result=result)
            log.info("job_succeeded", extra={"duration_s": round(time.perf_counter() - t0, 2)})
        except JobCancelled:
            repo.finish_job(job_id, "cancelled", error={"code": "JOB_CANCELLED", "message": "cancelled by user",
                                                       "retryable": False, "details": {}})
            log.info("job_cancelled")
        except AppError as exc:
            repo.finish_job(job_id, "failed", error=exc.to_dict()["error"])
            log.warning("job_failed", extra={"code": exc.code.value, "details": exc.details})
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            repo.update_job(job_id, worker_log=tb[-4000:])
            from app.services.render_service import SegmentQAError
            from app.utils.ffmpeg import FFmpegError
            code = (ErrorCode.RENDER_FAILED if isinstance(exc, FFmpegError)
                    else ErrorCode.OUTPUT_VALIDATION_FAILED if isinstance(exc, SegmentQAError) else ErrorCode.INTERNAL_ERROR)
            details = {"reason": type(exc).__name__, "message": str(exc)[:300]}
            if isinstance(exc, FFmpegError):
                details["ffmpeg_stderr_tail"] = exc.stderr[-600:]
            repo.finish_job(job_id, "failed", error=AppError(code, details=details).to_dict()["error"])
            log.exception("job_crashed")
        send_callback(job_id)
    finally:
        job_id_var.reset(t1)
        correlation_id_var.reset(t2)


def callback_payload(job_id: str) -> dict:
    from app.api.routes.jobs import job_status_payload

    return job_status_payload(repo.require_job(job_id))


def send_callback(job_id: str) -> None:
    s = get_settings()
    job = repo.get_job(job_id)
    url = (job.request_json or {}).get("callback_url")
    if not url:
        return
    body = json.dumps({"event": f"job.{job.status}", "job": callback_payload(job_id)}, default=str).encode()
    headers = {"Content-Type": "application/json", "X-Correlation-Id": job.correlation_id or ""}
    if s.callback_hmac_secret:
        headers["X-IncuBrix-Signature"] = sign_body(s.callback_hmac_secret, body)
    allowed = [h.strip() for h in s.callback_allowed_hosts.split(",") if h.strip()]
    for attempt in range(3):
        try:
            check_public_url(url, allowed or None)
            r = httpx.post(url, content=body, headers=headers, timeout=s.callback_timeout_s, follow_redirects=False)
            log.info("callback_sent", extra={"status": r.status_code})
            if r.status_code < 500:
                return
        except AppError as exc:
            log.warning("callback_blocked", extra={"reason": exc.details})
            return
        except httpx.HTTPError as exc:
            log.warning("callback_failed", extra={"attempt": attempt, "error": type(exc).__name__})
        time.sleep(2 ** attempt)
