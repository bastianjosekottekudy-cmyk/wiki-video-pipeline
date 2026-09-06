"""FastAPI local Wikipedia video library dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from src.config import (
    OUTPUT_DIR,
    add_topic_to_pool,
    load_pipeline_config,
    load_schedule_config,
    update_schedule_config,
)
from src.db import store
from src.naming import title_from_video_path
from src.scheduler import get_schedule_status, reload_daily_job
from src.topics import get_topics_status
from src.youtube.auth import (
    authorize_client_interactive,
    get_auth_session_status,
    probe_youtube_clients,
    start_auth_session,
    try_silent_refresh,
)

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

app = FastAPI(title="Wiki Video Library")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

_running_lock = threading.Lock()
_running_jobs: set[str] = set()
_generate_semaphore = threading.Semaphore(4)
_upload_lock = threading.Lock()
_uploading_runs: set[int] = set()


class ScheduleUpdateIn(BaseModel):
    enabled: bool | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    daily_topics_count: int | None = Field(default=None, ge=1, le=20)
    auto_upload: bool | None = None


class ScheduleRunNowIn(BaseModel):
    count: int | None = Field(default=None, ge=1, le=20)
    auto_upload: bool | None = None


class TopicPoolAddIn(BaseModel):
    topic: str


def _scheduled_daily_shorts() -> None:
    """Invoked by APScheduler daily cron trigger."""
    with _running_lock:
        if "daily_batch" in _running_jobs:
            logger.warning("Daily batch shorts already running; skipping duplicate trigger")
            return
        _running_jobs.add("daily_batch")

    def _bg() -> None:
        try:
            with _generate_semaphore:
                from src.pipeline import run_scheduled_shorts_batch

                run_scheduled_shorts_batch()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduled daily shorts batch failed: %s", exc)
        finally:
            with _running_lock:
                _running_jobs.discard("daily_batch")

    threading.Thread(target=_bg, daemon=True).start()


def _youtube_enabled() -> bool:
    return bool(load_pipeline_config().get("youtube", {}).get("enabled", False))


def _parse_json_field(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _duration(started: str | None, finished: str | None) -> str:
    if not started:
        return "—"
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(finished) if finished else datetime.now(timezone.utc)
        secs = int((end - start).total_seconds())
        mins, s = divmod(secs, 60)
        return f"{mins}m {s}s"
    except ValueError:
        return "—"


def _video_exists(run: dict[str, Any]) -> bool:
    path = run.get("video_path")
    return bool(path and Path(path).is_file())


def _safe_video_path(run: dict[str, Any]) -> Path:
    raw = run.get("video_path")
    if not raw:
        raise HTTPException(status_code=404, detail="No video for this run")
    path = Path(raw).resolve()
    output_root = OUTPUT_DIR.resolve()
    try:
        path.relative_to(output_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid video path") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found on disk")
    return path


def _run_output_dir(run: dict[str, Any]) -> Path | None:
    output_root = OUTPUT_DIR.resolve()
    run_id = run.get("id")
    run_date = run.get("run_date")
    fmt = run.get("format")

    if run_id and run_date and fmt:
        candidate = (OUTPUT_DIR / str(run_date) / str(fmt).lower() / f"run_{run_id}").resolve()
        try:
            candidate.relative_to(output_root)
        except ValueError:
            return None
        if candidate.is_dir():
            return candidate

    video_path = run.get("video_path")
    if video_path:
        path = Path(video_path).resolve()
        try:
            path.relative_to(output_root)
        except ValueError:
            return None
        parent = path.parent
        if parent.name.startswith("run_"):
            return parent
    return None


def _other_runs_use_path(run_id: int, directory: Path) -> bool:
    directory = directory.resolve()
    for other in store.list_runs(limit=500):
        if other.get("id") == run_id:
            continue
        for key in ("video_path", "script_path"):
            raw = other.get(key)
            if not raw:
                continue
            try:
                Path(raw).resolve().relative_to(directory)
                return True
            except ValueError:
                continue
    return False


def _delete_run_artifacts(run: dict[str, Any]) -> list[str]:
    deleted: list[str] = []
    run_id = int(run["id"])
    out_dir = _run_output_dir(run)

    if out_dir and out_dir.is_dir():
        if _other_runs_use_path(run_id, out_dir):
            logger.warning(
                "Skip folder delete for run %s — other runs share %s",
                run_id,
                out_dir,
            )
        else:
            shutil.rmtree(out_dir)
            deleted.append(str(out_dir))
            for parent in (out_dir.parent, out_dir.parent.parent):
                try:
                    if parent.is_dir() and parent.resolve() != OUTPUT_DIR.resolve():
                        if not any(parent.iterdir()):
                            parent.rmdir()
                            deleted.append(str(parent))
                except OSError:
                    pass
            return deleted

    for key in ("video_path", "script_path"):
        raw = run.get(key)
        if not raw:
            continue
        path = Path(raw).resolve()
        try:
            path.relative_to(OUTPUT_DIR.resolve())
        except ValueError:
            continue
        if path.is_file():
            path.unlink()
            deleted.append(str(path))
    return deleted


def _normalize_upload_status(run: dict[str, Any]) -> str:
    status = (run.get("upload_status") or "none").strip().lower()
    if status in ("uploading", "failed", "uploaded"):
        return status
    yt_id = (run.get("youtube_video_id") or "").strip()
    if yt_id and yt_id != "skipped":
        return "uploaded"
    return "none"


def _enrich_run(run: dict[str, Any]) -> dict[str, Any]:
    run["duration"] = _duration(run.get("started_at"), run.get("finished_at"))
    run["has_video"] = _video_exists(run)
    wiki_title = run.get("wiki_title") or run.get("topic") or ""
    fmt = str(run.get("format") or "short")
    run["video_title"] = title_from_video_path(
        run.get("video_path"),
        wiki_title=str(wiki_title),
        fmt=fmt,
        run_date=str(run.get("run_date") or ""),
    )
    upload_status = _normalize_upload_status(run)
    run["upload_status"] = upload_status
    yt_id = (run.get("youtube_video_id") or "").strip()
    run["is_uploaded"] = upload_status == "uploaded" and bool(yt_id) and yt_id != "skipped"
    run["youtube_url"] = (
        f"https://www.youtube.com/watch?v={yt_id}" if run["is_uploaded"] else ""
    )
    run["can_upload"] = bool(run["has_video"] and run.get("status") != "running")
    run["upload_label"] = (
        "Re-upload" if upload_status in ("uploaded", "failed") else "Upload"
    )
    run["format_label"] = "Short" if fmt == "short" else "Video"

    if upload_status == "uploading":
        run["display_status"] = "uploading"
    elif run["is_uploaded"]:
        run["display_status"] = "uploaded"
    elif upload_status == "failed" and run["has_video"]:
        run["display_status"] = "upload-failed"
    elif run.get("status") == "success" and not run["has_video"]:
        run["display_status"] = "missing"
    elif run.get("status") == "success" and run["has_video"]:
        run["display_status"] = "ready"
    else:
        run["display_status"] = run.get("status")
    return run


def _upload_run_video(run_id: int) -> None:
    run = store.get_run(run_id)
    if not run:
        return
    try:
        path = _safe_video_path(run)
    except HTTPException as exc:
        store.set_upload_status(run_id, "failed", upload_error=str(exc.detail))
        store.append_step_log(run_id, "upload", f"Upload failed: {exc.detail}")
        return
    from src.pipeline import attempt_youtube_upload

    attempt_youtube_upload(run_id, str(path))


def _retry_failed_uploads() -> None:
    """Re-attempt YouTube uploads that previously failed (capped to at most 10 per schedule)."""
    from src.scheduler import sync_failed_upload_retry_job

    if not _youtube_enabled():
        sync_failed_upload_retry_job()
        return

    failed = store.list_failed_uploads(limit=10)
    if not failed:
        sync_failed_upload_retry_job()
        return

    logger.info("Retrying %s failed YouTube upload(s) (limit 10)", len(failed))
    for run in failed:
        run_id = int(run["id"])
        if run.get("status") == "running":
            continue
        if not _video_exists(run):
            logger.warning(
                "Skipping upload retry for run %s — video file missing",
                run_id,
            )
            continue

        with _upload_lock:
            if run_id in _uploading_runs:
                continue
            _uploading_runs.add(run_id)

        store.set_upload_status(run_id, "uploading", upload_error=None)
        store.append_step_log(run_id, "upload", "Scheduled 6h retry of failed upload")
        try:
            _upload_run_video(run_id)
        except Exception:
            logger.exception("Scheduled 6h upload retry failed for run %s", run_id)
            current = store.get_run(run_id)
            if current and (current.get("upload_status") or "") == "uploading":
                store.set_upload_status(
                    run_id,
                    "failed",
                    upload_error="6h retry crashed unexpectedly",
                )
        finally:
            with _upload_lock:
                _uploading_runs.discard(run_id)

    sync_failed_upload_retry_job()


def _group_by_date(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for run in runs:
        date = run.get("run_date") or "unknown"
        grouped.setdefault(date, []).append(run)
    return [{"date": date, "runs": items} for date, items in grouped.items()]


def _job_key(topic: str, fmt: str) -> str:
    return f"{fmt}:{topic.strip().lower()}"


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    fmt: str | None = None,
    date: str | None = None,
    youtube_flash: str | None = None,
) -> HTMLResponse:
    runs = [_enrich_run(r) for r in store.list_runs(fmt=fmt, run_date=date)]
    groups = _group_by_date(runs)
    stats = store.count_runs_today()
    available_dates = store.list_run_dates()
    has_running = any(r["status"] == "running" for r in runs) or stats.get("running", 0) > 0
    has_uploading = (
        any(r.get("upload_status") == "uploading" for r in runs)
        or stats.get("uploading", 0) > 0
    )
    youtube_on = _youtube_enabled()
    youtube_clients: list[dict[str, Any]] = []
    if youtube_on:
        try:
            youtube_clients = probe_youtube_clients(attempt_refresh=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube client probe failed: %s", exc)
            youtube_clients = []
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "groups": groups,
            "stats": stats,
            "available_dates": available_dates,
            "filter_format": (fmt or "").lower(),
            "filter_date": date or "",
            "has_running": has_running,
            "has_uploading": has_uploading,
            "youtube_enabled": youtube_on,
            "youtube_clients": youtube_clients,
            "youtube_auth_warning": any(
                c.get("status") != "ok" for c in youtube_clients
            ),
            "youtube_flash": youtube_flash or "",
            "schedule": {
                **get_schedule_status(),
                "is_running": "daily_batch" in _running_jobs,
            },
            "topics": get_topics_status(),
        },
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int) -> HTMLResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _enrich_run(run)
    payload = _parse_json_field(run.get("article_json"))
    run["article"] = {}
    run["credits"] = []
    if isinstance(payload, dict):
        run["article"] = payload.get("article") or payload
        run["credits"] = payload.get("credits") or []
    run["steps"] = _parse_json_field(run.get("steps_log")) or []
    script_content = ""
    script_path = run.get("script_path")
    if script_path and Path(script_path).exists():
        script_content = Path(script_path).read_text(encoding="utf-8")
    run["script_content"] = script_content
    return templates.TemplateResponse(request, "run_detail.html", {"run": run})


@app.get("/api/youtube/status")
async def youtube_status() -> JSONResponse:
    if not _youtube_enabled():
        return JSONResponse({"enabled": False, "clients": []})
    return JSONResponse(
        {"enabled": True, "clients": probe_youtube_clients(attempt_refresh=False)}
    )


@app.post("/api/youtube/clients/{client_id}/refresh")
async def youtube_client_refresh(client_id: str) -> JSONResponse:
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        result = try_silent_refresh(client_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/youtube/clients/{client_id}/auth-status")
async def youtube_client_auth_status(client_id: str) -> JSONResponse:
    """Check status of an active or recent interactive OAuth session."""
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        return JSONResponse(get_auth_session_status(client_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/youtube/clients/{client_id}/start-auth")
async def youtube_client_start_auth(client_id: str) -> JSONResponse:
    """Start loopback listener with timeout and return auth_url for browser."""
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        return JSONResponse(start_auth_session(client_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/youtube/clients/{client_id}/authorize")
async def youtube_client_authorize(client_id: str) -> JSONResponse:
    """Start interactive OAuth session and return auth_url."""
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        session_info = start_auth_session(client_id)
        return JSONResponse(
            {
                "ok": True,
                "needs_browser": True,
                "auth_url": session_info.get("auth_url", ""),
                "status": session_info.get("status", "pending"),
                "detail": session_info.get("detail", "Sign in with Google in browser"),
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/youtube/clients/{client_id}/authorize")
async def youtube_client_authorize_redirect(client_id: str) -> RedirectResponse:
    """Redirect user's browser directly to Google OAuth sign-in."""
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        session_info = start_auth_session(client_id)
        return RedirectResponse(session_info["auth_url"], status_code=302)
    except Exception as exc:  # noqa: BLE001
        logger.warning("YouTube start-auth failed for %s: %s", client_id, exc)
        return RedirectResponse(
            "/?youtube_flash=" + quote(f"{client_id}: auth failed ({exc})"),
            status_code=302,
        )


@app.get("/videos/{run_id}/file")
async def video_file(run_id: int) -> FileResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    path = _safe_video_path(run)
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/videos/{run_id}/download")
async def video_download(run_id: int) -> FileResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    path = _safe_video_path(run)
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=path.name,
        content_disposition_type="attachment",
    )


def _run_is_uploaded(run: dict[str, Any]) -> bool:
    upload_status = _normalize_upload_status(run)
    yt_id = (run.get("youtube_video_id") or "").strip()
    return upload_status == "uploaded" and bool(yt_id) and yt_id != "skipped"


def _delete_run_if_idle(run: dict[str, Any]) -> dict[str, Any]:
    run_id = int(run["id"])
    if run.get("status") == "running":
        return {"run_id": run_id, "ok": False, "reason": "running"}
    if (run.get("upload_status") or "") == "uploading" or run_id in _uploading_runs:
        return {"run_id": run_id, "ok": False, "reason": "uploading"}
    deleted_paths = _delete_run_artifacts(run)
    store.delete_run(run_id)
    logger.info("Deleted run %s and artifacts: %s", run_id, deleted_paths)
    return {"run_id": run_id, "ok": True, "deleted_paths": deleted_paths}


@app.delete("/api/runs/{run_id}")
async def api_delete_run(run_id: int) -> JSONResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    result = _delete_run_if_idle(run)
    if not result["ok"]:
        reason = result.get("reason")
        if reason == "running":
            raise HTTPException(status_code=409, detail="Cannot delete a running job")
        raise HTTPException(status_code=409, detail="Cannot delete while uploading")
    return JSONResponse(
        {"ok": True, "run_id": run_id, "deleted_paths": result.get("deleted_paths", [])}
    )


@app.post("/api/runs/delete-bulk")
async def api_delete_runs_bulk(scope: str = "all") -> JSONResponse:
    scope_key = (scope or "all").strip().lower()
    if scope_key not in ("all", "uploaded"):
        raise HTTPException(status_code=400, detail="scope must be 'all' or 'uploaded'")
    runs = store.list_runs(limit=5000)
    if scope_key == "uploaded":
        runs = [r for r in runs if _run_is_uploaded(r)]
    deleted: list[int] = []
    skipped: list[dict[str, Any]] = []
    for run in runs:
        result = _delete_run_if_idle(run)
        if result["ok"]:
            deleted.append(int(result["run_id"]))
        else:
            skipped.append({"run_id": result["run_id"], "reason": result.get("reason")})
    return JSONResponse(
        {
            "ok": True,
            "scope": scope_key,
            "deleted_count": len(deleted),
            "deleted_ids": deleted,
            "skipped": skipped,
        }
    )


@app.post("/api/runs/{run_id}/upload")
async def api_upload_run(run_id: int, background_tasks: BackgroundTasks) -> JSONResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("status") == "running":
        raise HTTPException(status_code=409, detail="Wait for generation to finish")
    if not _video_exists(run):
        raise HTTPException(status_code=400, detail="No local video to upload")
    if (run.get("upload_status") or "") == "uploading":
        raise HTTPException(status_code=409, detail="Upload already in progress")

    with _upload_lock:
        if run_id in _uploading_runs:
            raise HTTPException(status_code=409, detail="Upload already in progress")
        _uploading_runs.add(run_id)

    store.set_upload_status(run_id, "uploading", upload_error=None)

    def _bg() -> None:
        try:
            _upload_run_video(run_id)
        finally:
            with _upload_lock:
                _uploading_runs.discard(run_id)

    background_tasks.add_task(_bg)
    return JSONResponse({"run_id": run_id, "status": "uploading"})


@app.get("/api/runs")
async def api_runs(fmt: str | None = None, date: str | None = None) -> JSONResponse:
    runs = [_enrich_run(r) for r in store.list_runs(fmt=fmt, run_date=date)]
    return JSONResponse({"runs": runs, "groups": _group_by_date(runs)})


@app.post("/api/generate")
async def api_generate(
    background_tasks: BackgroundTasks,
    topic: str,
    fmt: str = "short",
    mock: bool = False,
) -> JSONResponse:
    topic = (topic or "").strip()
    fmt = (fmt or "short").strip().lower()
    if not topic:
        raise HTTPException(status_code=400, detail="topic is required")
    if fmt not in ("short", "video"):
        raise HTTPException(status_code=400, detail="format must be short or video")

    key = _job_key(topic, fmt)
    with _running_lock:
        if key in _running_jobs:
            raise HTTPException(status_code=409, detail="That topic is already generating")

    def _bg() -> None:
        from src.pipeline import run_topic

        with _running_lock:
            _running_jobs.add(key)
        try:
            with _generate_semaphore:
                run_topic(
                    topic,
                    fmt,
                    skip_upload=not _youtube_enabled(),
                    mock=mock,
                )
        except Exception:
            logger.exception("Background generate failed for %s %s", fmt, topic)
        finally:
            with _running_lock:
                _running_jobs.discard(key)

    background_tasks.add_task(_bg)
    return JSONResponse({"status": "started", "topic": topic, "format": fmt})


@app.get("/api/schedule")
async def api_get_schedule() -> JSONResponse:
    """Get current schedule status, next run times, and topic count."""
    status = get_schedule_status()
    status["is_running"] = "daily_batch" in _running_jobs
    return JSONResponse(status)


@app.patch("/api/schedule")
async def api_patch_schedule(body: ScheduleUpdateIn) -> JSONResponse:
    """Update daily schedule configuration and re-arm scheduler."""
    update_schedule_config(
        enabled=body.enabled,
        hour=body.hour,
        minute=body.minute,
        daily_topics_count=body.daily_topics_count,
        auto_upload=body.auto_upload,
    )
    reload_daily_job()
    status = get_schedule_status()
    status["is_running"] = "daily_batch" in _running_jobs
    return JSONResponse(status)


@app.post("/api/schedule/run-now")
async def api_schedule_run_now(
    background_tasks: BackgroundTasks,
    body: ScheduleRunNowIn | None = None,
    count: int | None = Query(default=None, ge=1, le=20),
    auto_upload: bool | None = Query(default=None),
) -> JSONResponse:
    """Trigger the daily random shorts batch immediately in background using configured values."""
    target_count = (body and body.count) or count
    target_upload = (
        body.auto_upload
        if (body and body.auto_upload is not None)
        else auto_upload
    )

    if target_count is not None or target_upload is not None:
        update_schedule_config(
            daily_topics_count=target_count,
            auto_upload=target_upload,
        )
        reload_daily_job()

    cfg = load_schedule_config()
    effective_count = int(target_count or cfg.get("daily_topics_count") or 1)
    effective_auto_upload = bool(
        target_upload
        if target_upload is not None
        else cfg.get("auto_upload", True)
    )

    with _running_lock:
        if "daily_batch" in _running_jobs:
            raise HTTPException(
                status_code=409,
                detail="A batch generation is already running in the background. Please wait for it to complete.",
            )
        _running_jobs.add("daily_batch")

    def _bg(run_count: int, upload_flag: bool) -> None:
        try:
            with _generate_semaphore:
                from src.pipeline import run_scheduled_shorts_batch

                run_scheduled_shorts_batch(
                    count=run_count, auto_upload=upload_flag
                )
        except Exception:
            logger.exception("Manual run-now batch failed")
        finally:
            with _running_lock:
                _running_jobs.discard("daily_batch")

    background_tasks.add_task(_bg, effective_count, effective_auto_upload)
    return JSONResponse(
        {
            "status": "started",
            "count": effective_count,
            "auto_upload": effective_auto_upload,
            "message": f"Started scheduled batch for {effective_count} topic(s) in background",
        }
    )


@app.get("/api/topics")
async def api_get_topics() -> JSONResponse:
    """Get topic pool status, categories, and un-uploaded topics."""
    return JSONResponse(get_topics_status())


@app.post("/api/topics/pool")
async def api_add_topic(body: TopicPoolAddIn) -> JSONResponse:
    """Add a new topic to the configured pool."""
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Topic cannot be empty")
    add_topic_to_pool(topic)
    return JSONResponse(get_topics_status())


@app.post("/api/topics/refresh")
async def api_refresh_topics(count: int = Query(default=10, ge=1, le=30)) -> JSONResponse:
    """Discover fresh un-uploaded educational topics from Wikipedia & LLM to replenish the pool."""
    from src.topics.discovery import replenish_topics_pool

    added = replenish_topics_pool(count=count)
    status = get_topics_status()
    status["added_count"] = len(added)
    status["added_topics"] = added
    return JSONResponse(status)


def create_app() -> FastAPI:
    store.init_db()
    store.fail_orphaned_runs()
    return app
