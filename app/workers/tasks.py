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


def enqueue(job_id: str) -> None:
    global run_job
    if get_settings().job_runner == "inline":
        threading.Thread(target=execute, args=(job_id,), daemon=True, name=f"job-{job_id}").start()
        return
    if run_job is None:
        run_job = _celery_task()
    run_job.delay(job_id)


if get_settings().job_runner == "celery":
    run_job = _celery_task()
