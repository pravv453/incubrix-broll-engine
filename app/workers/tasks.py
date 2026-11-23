import threading

from app.core.config import get_settings
from app.workers.runner import execute


def _celery_task():
    from app.workers.celery_app import celery_app

    @celery_app.task(name="broll.run_job")
    def run_job(job_id: str) -> None:
        execute(job_id)

    return run_job


run_job = None
_slots: threading.BoundedSemaphore | None = None


def _run_inline(job_id: str) -> None:
    """Inline runner (tests, single-container demo): at most MAX_CONCURRENT_JOBS run at once;
    the rest stay 'queued' until a slot frees, like a Celery worker with that concurrency."""
    global _slots
    if _slots is None:
        _slots = threading.BoundedSemaphore(max(1, get_settings().max_concurrent_jobs))
    with _slots:
        execute(job_id)


def enqueue(job_id: str) -> None:
    global run_job
    if get_settings().job_runner == "inline":
        threading.Thread(target=_run_inline, args=(job_id,), daemon=True, name=f"job-{job_id}").start()
        return
    if run_job is None:
        run_job = _celery_task()
    run_job.delay(job_id)


if get_settings().job_runner == "celery":
    run_job = _celery_task()
