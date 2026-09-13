"""Topic → Wikipedia → narration → MP4 → optional YouTube upload."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any

# Ensure moviepy / imageio-ffmpeg uses system ffmpeg (supporting NVENC / hardware acceleration)
_system_ffmpeg = shutil.which("ffmpeg")
if _system_ffmpeg and "IMAGEIO_FFMPEG_EXE" not in os.environ:
    os.environ["IMAGEIO_FFMPEG_EXE"] = _system_ffmpeg

from src.audio.tts import generate_narration
from src.config import (
    format_profile,
    local_run_date,
    run_output_dir,
    should_delete_after_upload,
)
from src.db import store
from src.images.fetcher import fetch_article_images
from src.job_control import JobStoppedError, check_stop, register_run, unregister_run
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


def attempt_youtube_upload(
    run_id: int,
    video_path: str,
    delete_after_upload: bool | None = None,
    force_upload: bool = False,
) -> str | None:
    from src.youtube.uploader import YouTubeUploadError, upload_video

    run = store.get_run(run_id)
    if not run:
        return None

    topic_str = str(run.get("topic") or "").strip()
    wiki_title_str = str(run.get("wiki_title") or "").strip()

    # Prevent re-uploading an already uploaded topic
    if not force_upload:
        if (
            run.get("upload_status") == "uploaded"
            and run.get("youtube_video_id")
            and run.get("youtube_video_id") != "skipped"
        ):
            logger.info("Run %s already uploaded as %s; skipping re-upload", run_id, run.get("youtube_video_id"))
            return str(run.get("youtube_video_id"))

        if (
            store.is_topic_uploaded(topic_str, exclude_run_id=run_id)
            or (wiki_title_str and store.is_topic_uploaded(wiki_title_str, exclude_run_id=run_id))
        ):
            logger.warning("Topic %r is already uploaded to YouTube; skipping re-upload for run %s", topic_str, run_id)
            store.set_upload_status(run_id, "none", upload_error=None)
            store.append_step_log(run_id, "upload", f"Topic '{topic_str}' already uploaded to YouTube; skipped re-upload")
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
        store.record_uploaded_topic(
            str(run.get("topic") or ""),
            str(run.get("wiki_title") or article.get("title") or ""),
            run_id=run_id,
        )
        if should_delete_after_upload(delete_after_upload):
            try:
                p = Path(video_path)
                if p.is_file():
                    p.unlink()
                    logger.info(
                        "Deleted local video after upload for run %s: %s",
                        run_id,
                        video_path,
                    )
                    store.append_step_log(
                        run_id, "cleanup", f"Deleted local video: {p.name}"
                    )
                store.mark_run_dashboard_deleted(run_id)
                logger.info("Removed uploaded run %s from dashboard", run_id)
                store.append_step_log(
                    run_id, "cleanup", "Deleted uploaded item from dashboard"
                )
            except Exception as del_exc:
                logger.warning(
                    "Failed to delete local video/dashboard item for run %s (%s): %s",
                    run_id,
                    video_path,
                    del_exc,
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
    delete_after_upload: bool | None = None,
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
    store.update_run(run_id, status="running")
    out_dir = run_output_dir(run_date, fmt, run_id)
    register_run(run_id, topic)
    store.append_step_log(run_id, "start", f"{fmt} · {topic}")

    try:
        check_stop(run_id, topic)
        store.append_step_log(run_id, "wiki", "Resolving Wikipedia article")
        article = resolve_article(topic, fmt, out_dir, mock=mock)
        wiki_title = str(article.get("title") or topic).strip()
        store.update_run(
            run_id,
            wiki_title=wiki_title,
            wiki_url=article.get("url") or "",
        )
        store.append_step_log(run_id, "wiki", wiki_title)

        # Early check: if the resolved canonical Wikipedia article has already been uploaded, stop immediately
        if not force_upload and store.is_topic_uploaded(wiki_title, exclude_run_id=run_id):
            logger.warning(
                "Resolved Wikipedia article %r for topic %r has already been uploaded to YouTube; skipping generation for run %s",
                wiki_title,
                topic,
                run_id,
            )
            store.finish_run(run_id, "stopped", error_message=f"Article '{wiki_title}' already uploaded to YouTube")
            store.append_step_log(
                run_id,
                "wiki",
                f"Article '{wiki_title}' already uploaded to YouTube; skipped duplicate generation",
            )
            return run_id

        check_stop(run_id, topic)
        store.append_step_log(run_id, "images", "Fetching free images")
        credits = fetch_article_images(article, out_dir, mock=mock)
        check_stop(run_id, topic)
        payload = {"article": article, "credits": credits}
        store.update_run(run_id, article_json=json.dumps(payload))

        check_stop(run_id, topic)
        store.append_step_log(run_id, "script", "Writing narration")
        script = generate_script(article, fmt, out_dir)
        check_stop(run_id, topic)
        script_path = out_dir / "script.txt"
        store.update_run(run_id, script_path=str(script_path))

        check_stop(run_id, topic)
        store.append_step_log(run_id, "tts", "Generating speech")
        audio_path = generate_narration(script_path, out_dir)

        check_stop(run_id, topic)
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
        check_stop(run_id, topic)
        store.update_run(run_id, video_path=video_path)
        store.append_step_log(run_id, "render", Path(video_path).name)

        should_upload = force_upload or (not skip_upload and _youtube_enabled())
        if should_upload:
            check_stop(run_id, topic)
            attempt_youtube_upload(
                run_id,
                video_path,
                delete_after_upload=delete_after_upload,
                force_upload=force_upload,
            )
        else:
            store.append_step_log(run_id, "upload", "Skipped (local only)")

        store.finish_run(run_id, "success")
        if should_upload and should_delete_after_upload(delete_after_upload):
            curr_run = store.get_run(run_id)
            if curr_run and curr_run.get("upload_status") == "uploaded":
                store.mark_run_dashboard_deleted(run_id)
        logger.info("Run %s complete: %s", run_id, video_path)
        return run_id
    except JobStoppedError as exc:
        logger.warning("Run %s stopped: %s", run_id, exc)
        store.stop_run(run_id, reason="Stopped by user")
        raise
    except Exception as exc:
        logger.exception("Run %s failed", run_id)
        store.finish_run(run_id, "failed", error_message=str(exc))
        store.append_step_log(run_id, "error", str(exc))
        raise
    finally:
        unregister_run(run_id)


def retry_single_topic(
    run_id: int,
    *,
    mock: bool = False,
    skip_upload: bool = True,
    force_upload: bool = False,
    delete_after_upload: bool | None = None,
) -> int:
    """
    Retry a failed or stopped run, reusing its existing run record.
    """
    from src.job_control import clear_stop

    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")
    if run.get("status") == "running":
        raise ValueError(f"Run {run_id} is already running")

    topic = str(run.get("topic") or "").strip()
    fmt = str(run.get("format") or "short").strip()

    clear_stop(run_id, topic)
    store.reset_run_for_retry(run_id)

    return run_topic(
        topic,
        fmt=fmt,
        skip_upload=skip_upload,
        force_upload=force_upload,
        delete_after_upload=delete_after_upload,
        mock=mock,
        existing_run_id=run_id,
    )


def run_scheduled_shorts_batch(
    count: int | None = None,
    mock: bool = False,
    auto_upload: bool | None = None,
) -> list[int]:
    """
    Generate and upload a batch of random configured shorts.
    Picks un-uploaded topics, runs short pipeline, and uploads to YouTube.
    """
    from src.config import load_execution_config, load_schedule_config
    from src.topics.discovery import pick_random_topics

    sched = load_schedule_config()
    if count is None:
        count = int(sched.get("daily_topics_count") or 1)
    if auto_upload is None:
        auto_upload = bool(sched.get("auto_upload", True))

    exec_cfg = load_execution_config()
    max_workers = int(exec_cfg.get("max_concurrent_jobs", 5))

    topics = pick_random_topics(count)
    logger.info(
        "Starting scheduled shorts batch for %d topic(s) (concurrency: %d): %s",
        len(topics),
        max_workers,
        topics,
    )
    completed_run_ids: list[int] = []

    run_date = local_run_date()
    queued_items: list[tuple[int, str]] = []
    for topic in topics:
        if store.is_topic_uploaded(topic):
            logger.warning("Topic %r already has completed video/upload; skipping duplicate", topic)
            continue
        rid = store.create_run(topic, "short", run_date, status="queued")
        store.append_step_log(rid, "queued", f"Batch run queued for {topic}")
        queued_items.append((rid, topic))
        logger.info("Pre-created queued run %s for topic %r", rid, topic)

    def _process_item(rid: int, topic: str) -> int | None:
        try:
            check_stop(rid, topic)
        except JobStoppedError:
            logger.info("Run %s (%r) stopped by user while queued", rid, topic)
            store.stop_run(rid, reason="Stopped by user while queued")
            return None

        curr = store.get_run(rid)
        if curr and curr.get("status") == "stopped":
            logger.info("Run %s was stopped while queued; skipping", rid)
            return None

        try:
            logger.info("Scheduled batch: starting short for %r (run #%s)...", topic, rid)
            store.update_run(rid, status="running")
            run_id = run_topic(
                topic,
                fmt="short",
                skip_upload=not auto_upload,
                force_upload=auto_upload,
                mock=mock,
                existing_run_id=rid,
            )
            logger.info("Scheduled batch: completed run %s for %r", run_id, topic)
            return run_id
        except JobStoppedError:
            logger.warning("Scheduled batch run %s stopped for topic %r", rid, topic)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduled batch failed for topic %r (run %s): %s", topic, rid, exc)
            return None

    if max_workers > 1 and len(queued_items) > 1:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="batch_short"
        ) as executor:
            future_to_item = {
                executor.submit(_process_item, rid, topic): (rid, topic)
                for rid, topic in queued_items
            }
            for future in concurrent.futures.as_completed(future_to_item):
                rid, topic = future_to_item[future]
                try:
                    res = future.result()
                    if res is not None:
                        completed_run_ids.append(res)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Worker thread for run %s (%r) raised unexpected exception: %s",
                        rid,
                        topic,
                        exc,
                    )
    else:
        for rid, topic in queued_items:
            res = _process_item(rid, topic)
            if res is not None:
                completed_run_ids.append(res)

    # Cancel any remaining runs in queued_items that were never reached or finished
    for rem_id, rem_topic in queued_items:
        if rem_id not in completed_run_ids:
            run_data = store.get_run(rem_id)
            if run_data and run_data.get("status") == "queued":
                store.stop_run(rem_id, reason="Stopped before processing started")

    return completed_run_ids


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
    parser.add_argument(
        "--delete-after-upload",
        action="store_true",
        default=None,
        help="Delete local video file after successful YouTube upload (default: true)",
    )
    parser.add_argument(
        "--keep-video",
        "--no-delete-after-upload",
        dest="delete_after_upload",
        action="store_false",
        help="Keep local video file after YouTube upload (do not auto-delete)",
    )
    parser.add_argument("--mock", action="store_true", help="Skip Wikipedia; placeholder images")
    args = parser.parse_args()

    store.init_db()
    run_topic(
        args.topic,
        args.fmt,
        skip_upload=not args.upload,
        force_upload=args.upload,
        delete_after_upload=args.delete_after_upload,
        mock=args.mock,
    )


if __name__ == "__main__":
    main()
