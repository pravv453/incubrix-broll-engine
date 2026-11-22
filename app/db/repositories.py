"""Job repository: idempotent creation, monotonic progress, terminal transitions."""
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import AppError, ErrorCode
from app.db.models import Job
from app.db.session import session_scope

TERMINAL = {"succeeded", "failed", "cancelled"}


def new_job_id(prefix: str = "broll") -> str:
    # time-ordered, url-safe: ms timestamp (base36) + 10 random chars
    ts = int(time.time() * 1000)
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    t36 = ""
    while ts:
        ts, r = divmod(ts, 36)
        t36 = alphabet[r] + t36
    return f"{prefix}_{t36}{secrets.token_hex(5)}"


def canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def create_or_get_job(
    *, idempotency_key: str, request_json: dict, kind: str = "broll", parent_job_id: str | None = None,
    correlation_id: str | None = None, job_id: str | None = None,
) -> tuple[Job, bool]:
    """Returns (job, created). Same key + same body -> existing job; same key + other body -> 409.

    The unique constraint on idempotency_key makes this safe under concurrent
    requests: the loser of the insert race reads the winner's row."""
    req_hash = canonical_hash({"kind": kind, "parent": parent_job_id, "request": request_json})
    existing = get_by_idempotency_key(idempotency_key)
    if existing:
        return _check_same(existing, req_hash), False
    job = Job(
        id=job_id or new_job_id("swap" if kind == "swap" else "broll"),
        kind=kind,
        parent_job_id=parent_job_id,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
        request_json=request_json,
        correlation_id=correlation_id,
        warnings=[],
    )
    try:
        with session_scope() as s:
            s.add(job)
        return job, True
    except IntegrityError:
        existing = get_by_idempotency_key(idempotency_key)
        if existing is None:
            raise
        return _check_same(existing, req_hash), False


def _check_same(job: Job, req_hash: str) -> Job:
    if job.request_hash != req_hash:
        raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, details={"existing_job_id": job.id})
    return job


def get_by_idempotency_key(key: str) -> Job | None:
    with session_scope() as s:
        return s.scalar(select(Job).where(Job.idempotency_key == key))


def get_job(job_id: str) -> Job | None:
    with session_scope() as s:
        return s.get(Job, job_id)


def require_job(job_id: str) -> Job:
    job = get_job(job_id)
    if job is None:
        raise AppError(ErrorCode.JOB_NOT_FOUND, details={"job_id": job_id})
    return job


def update_job(job_id: str, **fields: Any) -> Job:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise AppError(ErrorCode.JOB_NOT_FOUND)
        if "progress" in fields:  # progress is monotonic, clamped to 0-100
            fields["progress"] = max(job.progress, min(100, int(fields["progress"])))
        for k, v in fields.items():
            setattr(job, k, v)
        return job


def add_warning(job_id: str, warning: dict) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is not None:
            job.warnings = [*(job.warnings or []), warning]


def mark_running(job_id: str) -> Job:
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status = "running"
        job.attempts = (job.attempts or 0) + 1
        job.started_at = job.started_at or datetime.now(timezone.utc)
        return job


def finish_job(job_id: str, status: str, *, result: dict | None = None, error: dict | None = None) -> Job:
    fields: dict[str, Any] = {"status": status, "finished_at": datetime.now(timezone.utc)}
    if status == "succeeded":
        fields.update(progress=100, stage="completed", result=result)
    else:
        fields.update(stage=status, error=error)
    return update_job(job_id, **fields)
