"""Entry point: FastAPI dashboard (on-demand, no daily scheduler)."""

from __future__ import annotations

import logging

import uvicorn

from src.config import load_pipeline_config
from src.db import store
from src.web.app import app

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
    logger.info("Dashboard: http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
