"""APScheduler: daily random shorts job (IST) + conditional failed-upload retries."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import load_schedule_config

logger = logging.getLogger(__name__)

DAILY_SHORTS_JOB_ID = "daily-random-shorts"
UPLOAD_RETRY_JOB_ID = "retry-failed-uploads"
UPLOAD_RETRY_INTERVAL_HOURS = 6
UPLOAD_RETRY_MAX_BATCH_SIZE = 10

_scheduler: BackgroundScheduler | None = None
_daily_shorts_callback: Callable[[], None] | None = None
_retry_uploads_callback: Callable[[], None] | None = None


def _format_local(dt: datetime) -> str:
    local = dt.astimezone()
    return local.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def _format_time_label(hour: int, minute: int, tz_name: str = "IST") -> str:
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {suffix} {tz_name}"


def reload_daily_job() -> None:
    """Re-arm daily shorts job according to latest pipeline.yaml configuration."""
    if not _scheduler or not _scheduler.running or _daily_shorts_callback is None:
        return

    cfg = load_schedule_config()
    existing = _scheduler.get_job(DAILY_SHORTS_JOB_ID)

    if not cfg.get("enabled", True):
        if existing is not None:
            _scheduler.remove_job(DAILY_SHORTS_JOB_ID)
            logger.info("Daily shorts job disabled and unscheduled")
        return

    hour = int(cfg.get("hour", 20))
    minute = int(cfg.get("minute", 0))
    tz_str = str(cfg.get("timezone", "Asia/Kolkata"))

    trigger = CronTrigger(hour=hour, minute=minute, timezone=tz_str)
    _scheduler.add_job(
        _daily_shorts_callback,
        trigger=trigger,
        id=DAILY_SHORTS_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    job = _scheduler.get_job(DAILY_SHORTS_JOB_ID)
    next_time = job.next_run_time if job else None
    next_label = _format_local(next_time) if next_time else "soon"
    logger.info(
        "Scheduled daily shorts at %s (next: %s, %d topic(s))",
        _format_time_label(hour, minute, "IST"),
        next_label,
        int(cfg.get("daily_topics_count") or 1),
    )


def get_schedule_status() -> dict[str, Any]:
    """Inspect current schedule state, next run time, and topic batch size."""
    cfg = load_schedule_config()
    enabled = bool(cfg.get("enabled", True))
    hour = int(cfg.get("hour", 20))
    minute = int(cfg.get("minute", 0))
    daily_count = int(cfg.get("daily_topics_count", 1))
    tz_str = str(cfg.get("timezone", "Asia/Kolkata"))

    next_run_str = ""
    next_run_local = ""
    countdown = ""

    if _scheduler and _scheduler.running:
        job = _scheduler.get_job(DAILY_SHORTS_JOB_ID)
        if job and job.next_run_time:
            nxt = job.next_run_time
            next_run_str = nxt.isoformat()
            next_run_local = _format_local(nxt)
            diff = nxt - datetime.now(nxt.tzinfo)
            if diff.total_seconds() > 0:
                hours, remainder = divmod(int(diff.total_seconds()), 3600)
                mins, _ = divmod(remainder, 60)
                if hours > 0:
                    countdown = f"in {hours}h {mins}m"
                else:
                    countdown = f"in {mins}m"
            else:
                countdown = "due now"

    return {
        "enabled": enabled,
        "hour": hour,
        "minute": minute,
        "time_ist": f"{hour:02d}:{minute:02d}",
        "time_label": _format_time_label(hour, minute, "IST"),
        "timezone": tz_str,
        "daily_topics_count": daily_count,
        "auto_upload": bool(cfg.get("auto_upload", True)),
        "next_run_time": next_run_str,
        "next_run_local": next_run_local,
        "countdown": countdown,
    }


def sync_failed_upload_retry_job(*, run_in_hours: float | None = None) -> None:
    """Keep the 6-hour retry job only while failed uploads exist."""
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
    daily_shorts_callback: Callable[[], None] | None = None,
    retry_uploads_callback: Callable[[], None] | None = None,
) -> BackgroundScheduler:
    """Start the background scheduler and arm daily shorts & upload retry jobs."""
    global _scheduler, _daily_shorts_callback, _retry_uploads_callback
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler()
    _daily_shorts_callback = daily_shorts_callback
    _retry_uploads_callback = retry_uploads_callback
    scheduler.start()
    _scheduler = scheduler

    if daily_shorts_callback is not None:
        reload_daily_job()

    if retry_uploads_callback is not None:
        sync_failed_upload_retry_job()

    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler, _daily_shorts_callback, _retry_uploads_callback
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    _daily_shorts_callback = None
    _retry_uploads_callback = None
