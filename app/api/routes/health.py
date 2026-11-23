"""Liveness and readiness (Integration Standard #8).

/health  - process is up; never touches dependencies.
/ready   - DB, queue and object storage reachable; 503 + standard error otherwise.
Dependency checks are registered on app.state so tests can substitute them.
"""
from collections.abc import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

router = APIRouter(tags=["health"])

ReadinessCheck = Callable[[], None]  # raises on failure


@router.get("/health", summary="Liveness probe")
def health() -> dict[str, str]:
    s = get_settings()
    return {"status": "ok", "version": s.app_version}


@router.get("/ready", summary="Readiness probe (DB, queue, storage)")
def ready(request: Request) -> JSONResponse:
    checks: dict[str, ReadinessCheck] = getattr(request.app.state, "readiness_checks", {})
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            check()
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
            results[name] = f"error: {type(exc).__name__}"
    if any(v != "ok" for v in results.values()):
        err = AppError(ErrorCode.SERVICE_NOT_READY, details={"checks": results})
        return JSONResponse(status_code=err.http_status, content=err.to_dict())
    return JSONResponse(status_code=200, content={"status": "ready", "checks": results})


def default_readiness_checks() -> dict[str, ReadinessCheck]:
    """Real dependency probes, each with a short timeout."""
    s = get_settings()

    def db() -> None:
        from sqlalchemy import create_engine, text

        args = {} if s.database_url.startswith("sqlite") else {"connect_timeout": 3}
        engine = create_engine(s.database_url, pool_pre_ping=True, connect_args=args)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()

    def queue() -> None:
        import redis

        redis.Redis.from_url(s.redis_url, socket_connect_timeout=3, socket_timeout=3).ping()

    def storage() -> None:
        from app.storage.s3_storage import S3Storage, get_storage

        st = get_storage()
        if isinstance(st, S3Storage):
            st.client.head_bucket(Bucket=st.bucket)

    checks = {"database": db, "queue": queue, "storage": storage}
    if s.job_runner == "inline":  # single-container mode: jobs run in-process, there is no broker to probe
        checks.pop("queue")
    return checks
