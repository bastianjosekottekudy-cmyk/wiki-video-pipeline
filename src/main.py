"""Entry point: FastAPI dashboard + failed-upload retry job."""

from __future__ import annotations

import logging

import uvicorn

from src.config import load_pipeline_config
from src.db import store
from src.scheduler import shutdown_scheduler, start_scheduler
from src.web.app import _retry_failed_uploads, app

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

    start_scheduler(retry_uploads_callback=_retry_failed_uploads)
    failed_uploads = store.count_failed_uploads()
    if failed_uploads:
        logger.info(
            "%s failed YouTube upload(s) pending — retry job armed (hourly while failures remain)",
            failed_uploads,
        )
    else:
        logger.info("No failed YouTube uploads — retry job not scheduled")
    logger.info("Dashboard: http://%s:%s", host, port)

    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        shutdown_scheduler()


if __name__ == "__main__":
    main()
