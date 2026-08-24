"""Topic → Wikipedia → narration → MP4 → optional YouTube upload."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from src.audio.tts import generate_narration
from src.config import format_profile, local_run_date, run_output_dir
from src.db import store
from src.images.fetcher import fetch_article_images
from src.script.generator import generate_script
from src.video.renderer import render_video
from src.wiki.fetcher import resolve_article

logger = logging.getLogger(__name__)


def _youtube_enabled() -> bool:
    from src.youtube.uploader import youtube_enabled

    return youtube_enabled()


def _load_article_payload(run: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = run.get("article_json")
    data: Any = {}
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
    if isinstance(data, dict) and data.get("article"):
        article = data.get("article") or {}
        credits = data.get("credits") or []
        return article, credits if isinstance(credits, list) else []
    if isinstance(data, dict):
        return data, []
    return {}, []


def attempt_youtube_upload(run_id: int, video_path: str) -> str | None:
    from src.youtube.uploader import YouTubeUploadError, upload_video

    run = store.get_run(run_id)
    if not run:
        return None
    article, credits = _load_article_payload(run)
    if not article:
        article = {
            "title": run.get("wiki_title") or run.get("topic") or "Wikipedia",
            "url": run.get("wiki_url") or "",
            "topic": run.get("topic") or "",
        }
    store.set_upload_status(run_id, "uploading", upload_error=None)
    store.append_step_log(run_id, "upload", "Uploading to YouTube")
    try:
        youtube_id = upload_video(
            video_path,
            article,
            credits,
            str(run.get("format") or "short"),
            str(run.get("run_date") or local_run_date()),
        )
        store.set_upload_status(
            run_id, "uploaded", youtube_video_id=youtube_id, upload_error=None
        )
        store.append_step_log(
            run_id, "upload", f"Uploaded https://www.youtube.com/watch?v={youtube_id}"
        )
        return youtube_id
    except YouTubeUploadError as exc:
        msg = str(exc)
        if "upload skipped" in msg.lower():
            logger.info("YouTube upload skipped for run %s: %s", run_id, exc)
            store.set_upload_status(run_id, "none", upload_error=None)
            store.append_step_log(run_id, "upload", msg)
            return None
        logger.warning("YouTube upload failed for run %s: %s", run_id, exc)
        store.set_upload_status(run_id, "failed", upload_error=msg)
        store.append_step_log(run_id, "upload", f"Upload failed: {exc}")
        from src.scheduler import sync_failed_upload_retry_job

        sync_failed_upload_retry_job()
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected YouTube upload error for run %s", run_id)
        store.set_upload_status(run_id, "failed", upload_error=str(exc))
        store.append_step_log(run_id, "upload", f"Upload failed: {exc}")
        from src.scheduler import sync_failed_upload_retry_job

        sync_failed_upload_retry_job()
        return None


def run_topic(
    topic: str,
    fmt: str = "short",
    *,
    skip_upload: bool = True,
    force_upload: bool = False,
    mock: bool = False,
    existing_run_id: int | None = None,
) -> int:
    fmt = (fmt or "short").strip().lower()
    format_profile(fmt)  # validate
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic is required")

    run_date = local_run_date()
    run_id = existing_run_id or store.create_run(topic, fmt, run_date)
    out_dir = run_output_dir(run_date, fmt, run_id)
    store.append_step_log(run_id, "start", f"{fmt} · {topic}")

    try:
        store.append_step_log(run_id, "wiki", "Resolving Wikipedia article")
        article = resolve_article(topic, fmt, out_dir, mock=mock)
        store.update_run(
            run_id,
            wiki_title=article.get("title") or topic,
            wiki_url=article.get("url") or "",
        )
        store.append_step_log(run_id, "wiki", str(article.get("title") or topic))

        store.append_step_log(run_id, "images", "Fetching free images")
        credits = fetch_article_images(article, out_dir, mock=mock)
        payload = {"article": article, "credits": credits}
        store.update_run(run_id, article_json=json.dumps(payload))

        store.append_step_log(run_id, "script", "Writing narration")
        script = generate_script(article, fmt, out_dir)
        script_path = out_dir / "script.txt"
        store.update_run(run_id, script_path=str(script_path))

        store.append_step_log(run_id, "tts", "Generating speech")
        audio_path = generate_narration(script_path, out_dir)

        store.append_step_log(run_id, "render", f"Rendering {fmt}")
        video_path = render_video(
            str(article.get("title") or topic),
            fmt,
            run_date,
            audio_path,
            out_dir,
            script=script,
            image_paths=[c["path"] for c in credits if c.get("path")],
        )
        store.update_run(run_id, video_path=video_path)
        store.append_step_log(run_id, "render", Path(video_path).name)

        should_upload = force_upload or (not skip_upload and _youtube_enabled())
        if should_upload:
            attempt_youtube_upload(run_id, video_path)
        else:
            store.append_step_log(run_id, "upload", "Skipped (local only)")

        store.finish_run(run_id, "success")
        logger.info("Run %s complete: %s", run_id, video_path)
        return run_id
    except Exception as exc:
        logger.exception("Run %s failed", run_id)
        store.finish_run(run_id, "failed", error_message=str(exc))
        store.append_step_log(run_id, "error", str(exc))
        raise


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Wikipedia narrative video pipeline")
    parser.add_argument("--topic", required=True, help="Topic to look up on Wikipedia")
    parser.add_argument(
        "--format",
        dest="fmt",
        choices=("short", "video"),
        default="short",
        help="short = 9:16 up to 180s; video = 16:9 uncapped",
    )
    parser.add_argument("--upload", action="store_true", help="Upload to YouTube after render")
    parser.add_argument("--mock", action="store_true", help="Skip Wikipedia; placeholder images")
    args = parser.parse_args()

    store.init_db()
    run_topic(
        args.topic,
        args.fmt,
        skip_upload=not args.upload,
        force_upload=args.upload,
        mock=args.mock,
    )


if __name__ == "__main__":
    main()
