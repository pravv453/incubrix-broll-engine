"""FastAPI application factory."""
import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse

from app.api.routes import broll, health, jobs
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, install_error_handlers
from app.core.logging import configure_logging, correlation_id_var
from app.core.security import RateLimiter
from app.schemas.requests import BrollJobOptions, BrollJobRequest

log = logging.getLogger("app.http")

CORRELATION_HEADER = "X-Correlation-Id"
DEMO_PAGE = Path(__file__).parent / "static" / "demo.html"


def create_app(readiness_checks: dict | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="IncuBrix B-roll Engine",
        version=settings.app_version,
        description=(
            "Turns a voiceover or script into licence-clean B-roll videos, scene by scene, with narration-timed "
            "captions, timeline.json and manifest.json. Long work runs as background jobs. All errors use "
            '`{"error": {"code", "message", "retryable", "details"}}`.'
        ),
    )
    install_error_handlers(app)

    if readiness_checks is None:
        readiness_checks = health.default_readiness_checks()
    app.state.readiness_checks = readiness_checks
    limiter = RateLimiter(settings.rate_limit_per_minute)

    @app.middleware("http")
    async def correlation_and_guard(request: Request, call_next):
        cid = (request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4()))[:128]
        token = correlation_id_var.set(cid)
        start = time.perf_counter()
        try:
            path = request.url.path
            err: AppError | None = None
            headers: dict[str, str] = {}
            if path.startswith("/api/v1/") and settings.demo_access_token and not path.startswith("/api/v1/files/"):
                auth = request.headers.get("authorization", "")
                if auth != f"Bearer {settings.demo_access_token}":
                    err = AppError(ErrorCode.UNAUTHORIZED)
            if err is None and request.method == "POST" and path.startswith("/api/v1/"):
                client = request.client.host if request.client else "unknown"
                wait = limiter.check(client)
                if wait is not None:
                    err = AppError(ErrorCode.RATE_LIMITED, details={"retry_after_s": wait})
                    headers["Retry-After"] = str(int(wait) + 1)
            if err is not None:
                response = JSONResponse(status_code=err.http_status, content=err.to_dict(), headers=headers)
            else:
                response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers[CORRELATION_HEADER] = cid
        log.info("request", extra={"correlation_id": cid, "method": request.method, "path": request.url.path,
                                   "status": response.status_code,
                                   "duration_ms": round((time.perf_counter() - start) * 1000, 1)})
        return response

    app.include_router(health.router)
    app.include_router(broll.router)
    app.include_router(jobs.router)

    @app.get("/", include_in_schema=False)
    def demo_page():
        return FileResponse(DEMO_PAGE) if DEMO_PAGE.exists() else JSONResponse({"docs": "/docs"})

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        comps = schema.setdefault("components", {}).setdefault("schemas", {})
        for model in (BrollJobRequest, BrollJobOptions):
            js = model.model_json_schema(ref_template="#/components/schemas/{model}")
            for name, d in js.pop("$defs", {}).items():
                comps.setdefault(name, d)
            comps[model.__name__] = js
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    return app


app = create_app()  # uvicorn app.main:app
