"""Entry point: FastAPI dashboard + failed-upload retry job."""

from __future__ import annotations

import logging
import os
import shutil

# Ensure moviepy / imageio-ffmpeg uses system ffmpeg (supporting NVENC / hardware acceleration)
_system_ffmpeg = shutil.which("ffmpeg")
if _system_ffmpeg and "IMAGEIO_FFMPEG_EXE" not in os.environ:
    os.environ["IMAGEIO_FFMPEG_EXE"] = _system_ffmpeg

import uvicorn

from src.config import load_pipeline_config
from src.db import store
from src.scheduler import (
    FAILED_RUNS_RETRY_INTERVAL_HOURS,
    UPLOAD_RETRY_INTERVAL_HOURS,
    shutdown_scheduler,
    start_scheduler,
)
from src.web.app import _retry_failed_runs, _retry_failed_uploads, _scheduled_daily_shorts, app

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    store.init_db()
    orphaned = store.fail_orphaned_runs()
    if orphaned:
        logger.warning(
            "Marked %s orphaned run(s) as failed after restart: %s",
            len(orphaned),
            orphaned,
        )

    config = load_pipeline_config()
    web_cfg = config.get("web", {})
    host = web_cfg.get("host", "127.0.0.1")
    port = int(web_cfg.get("port", 8082))

    start_scheduler(
        daily_shorts_callback=_scheduled_daily_shorts,
        retry_uploads_callback=_retry_failed_uploads,
        retry_failed_runs_callback=_retry_failed_runs,
    )
    failed_uploads = store.count_failed_uploads()
    if failed_uploads:
        logger.info(
            "%s failed YouTube upload(s) pending — retry job armed (every %sh, max 10 per run while failures remain)",
            failed_uploads,
            UPLOAD_RETRY_INTERVAL_HOURS,
        )
    else:
        logger.info("No failed YouTube uploads — retry job not scheduled")

    recent_failed = store.count_recent_failed_runs(hours=24)
    if recent_failed:
        logger.info(
            "%s failed run(s) from last 24h pending — 1-hour generation retry job armed (every %sh)",
            recent_failed,
            FAILED_RUNS_RETRY_INTERVAL_HOURS,
        )
    else:
        logger.info("No recent failed runs — 1-hour generation retry job idle")

    logger.info("Dashboard: http://%s:%s", host, port)

    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        shutdown_scheduler()


if __name__ == "__main__":
    main()
