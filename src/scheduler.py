"""Conditional 6-hour retry of failed YouTube uploads (no daily generate jobs)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

UPLOAD_RETRY_JOB_ID = "retry-failed-uploads"
UPLOAD_RETRY_INTERVAL_HOURS = 6
UPLOAD_RETRY_MAX_BATCH_SIZE = 10

_scheduler: BackgroundScheduler | None = None
_retry_uploads_callback: Callable[[], None] | None = None


def _format_local(dt: datetime) -> str:
    local = dt.astimezone()
    return local.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def sync_failed_upload_retry_job(*, run_in_hours: float | None = None) -> None:
    """
    Keep the 6-hour retry job only while failed uploads exist.
    Does nothing if the scheduler or retry callback is not ready.
    """
    if not _scheduler or not _scheduler.running or _retry_uploads_callback is None:
        return

    from src.db import store

    has_failed = store.count_failed_uploads() > 0
    existing = _scheduler.get_job(UPLOAD_RETRY_JOB_ID)

    if not has_failed:
        if existing is not None:
            _scheduler.remove_job(UPLOAD_RETRY_JOB_ID)
            logger.info("Cleared upload-retry job — no failed uploads")
        return

    delay_h = (
        UPLOAD_RETRY_INTERVAL_HOURS if run_in_hours is None else max(0.0, float(run_in_hours))
    )
    next_run = datetime.now().astimezone() + timedelta(hours=delay_h)

    if existing is None:
        _scheduler.add_job(
            _retry_uploads_callback,
            trigger=IntervalTrigger(hours=UPLOAD_RETRY_INTERVAL_HOURS),
            id=UPLOAD_RETRY_JOB_ID,
            replace_existing=True,
            misfire_grace_time=1800,
            next_run_time=next_run,
        )
        logger.info(
            "Scheduled upload retry every %sh (next %s) — failed uploads pending",
            UPLOAD_RETRY_INTERVAL_HOURS,
            _format_local(next_run),
        )


def start_scheduler(
    *,
    retry_uploads_callback: Callable[[], None] | None = None,
) -> BackgroundScheduler:
    """Start the background scheduler and arm upload retries if any failures exist."""
    global _scheduler, _retry_uploads_callback
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler()
    _retry_uploads_callback = retry_uploads_callback
    scheduler.start()
    _scheduler = scheduler

    if retry_uploads_callback is not None:
        sync_failed_upload_retry_job()

    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler, _retry_uploads_callback
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    _retry_uploads_callback = None
