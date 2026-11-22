"""Structured JSON logging with job_id and correlation id (Integration Standard #8).

`job_id` and `correlation_id` live in contextvars so any log line emitted while
handling a request or running a job carries them automatically - in the API
process and in the Celery worker alike.
"""
import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_REDACT_KEYS = {"api_key", "secret", "token", "authorization", "signature", "password"}


def _redact(key: str, value: object) -> object:
    return "[REDACTED]" if any(k in key.lower() for k in _REDACT_KEYS) else value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "job_id": getattr(record, "job_id", None) or job_id_var.get(),
            "correlation_id": getattr(record, "correlation_id", None) or correlation_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = _redact(key, value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).handlers[:] = []
        logging.getLogger(noisy).propagate = True
