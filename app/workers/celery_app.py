from celery import Celery
from celery.signals import worker_process_init

from app.core.config import get_settings
from app.core.logging import configure_logging

s = get_settings()
celery_app = Celery("broll", broker=s.redis_url, include=["app.workers.tasks"])
celery_app.conf.update(
    task_acks_late=True,  # a crashed worker's job is redelivered, not lost
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=s.max_concurrent_jobs,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    worker_hijack_root_logger=False,
)


@worker_process_init.connect
def _init_logging(**_):
    configure_logging(get_settings().log_level)
