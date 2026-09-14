"""FastAPI local Wikipedia video library dashboard."""

from __future__ import annotations

import asyncio
import concurrent.futures
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
    load_execution_config,
    load_pipeline_concurrency,
    load_pipeline_config,
    load_schedule_config,
    should_delete_after_upload,
    update_delete_after_upload,
    update_pipeline_concurrency,
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
_upload_lock = threading.Lock()
_uploading_runs: set[int] = set()


class ConcurrencyLimiter:
    """Thread-safe dynamic concurrency limiter supporting runtime limit updates."""

    def __init__(self, limit: int = 5):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._active = 0
        self._limit = max(1, int(limit))

    @property
    def limit(self) -> int:
        with self._lock:
            return self._limit

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active

    def set_limit(self, new_limit: int) -> None:
        with self._cv:
            self._limit = max(1, int(new_limit))
            self._cv.notify_all()

    def acquire(self) -> None:
        with self._cv:
            while self._active >= self._limit:
                self._cv.wait()
            self._active += 1

    def release(self) -> None:
        with self._cv:
            self._active = max(0, self._active - 1)
            self._cv.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


_concurrency_init = load_pipeline_concurrency()
_generate_semaphore = ConcurrencyLimiter(_concurrency_init["effective_limit"])


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
    run["can_upload"] = bool(run["has_video"] and run.get("status") not in ("running", "queued"))
    run["can_stop"] = bool(run.get("status") in ("running", "queued"))
    run["can_retry"] = bool(run.get("status") not in ("running", "queued"))
    run["upload_label"] = (
        "Re-upload" if upload_status in ("uploaded", "failed") else "Upload"
    )
    run["format_label"] = "Short" if fmt == "short" else "Video"

    if run.get("status") == "queued":
        run["display_status"] = "queued"
    elif upload_status == "uploading":
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

    attempt_youtube_upload(run_id, str(path), delete_after_upload=should_delete_after_upload())


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
    norm = store.normalize_topic_key(topic)
    return f"{fmt}:{norm}" if norm else f"{fmt}:{topic.strip().lower()}"


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
    has_queued = any(r.get("status") == "queued" for r in runs) or stats.get("queued", 0) > 0
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
    conc = load_pipeline_concurrency()
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
            "has_queued": has_queued,
            "has_uploading": has_uploading,
            "concurrency_enabled": conc["concurrency_enabled"],
            "max_parallel_jobs": conc["max_parallel_jobs"],
            "effective_concurrency": _generate_semaphore.limit,
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
            "delete_after_upload": should_delete_after_upload(),
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
    if run.get("status") in ("running", "queued"):
        return {"run_id": run_id, "ok": False, "reason": run.get("status")}
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
        if reason in ("running", "queued"):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete a {reason} job. Stop or cancel it first.",
            )
        raise HTTPException(status_code=409, detail="Cannot delete while uploading")
    return JSONResponse(
        {"ok": True, "run_id": run_id, "deleted_paths": result.get("deleted_paths", [])}
    )


@app.post("/api/runs/delete-bulk")
async def api_delete_runs_bulk(scope: str = "all") -> JSONResponse:
    scope_key = (scope or "all").strip().lower()
    if scope_key not in ("all", "uploaded", "failed"):
        raise HTTPException(status_code=400, detail="scope must be 'all', 'uploaded', or 'failed'")
    runs = store.list_runs(limit=5000)
    if scope_key == "uploaded":
        runs = [r for r in runs if _run_is_uploaded(r)]
    elif scope_key == "failed":
        runs = [r for r in runs if r.get("status") in ("failed", "stopped")]
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


@app.post("/api/runs/delete-failed")
async def api_delete_failed_runs() -> JSONResponse:
    """Delete all failed and stopped runs from disk and database."""
    return await api_delete_runs_bulk(scope="failed")


@app.post("/api/runs/{run_id}/stop")
def api_stop_run(run_id: int) -> JSONResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("status") not in ("running", "queued"):
        return JSONResponse({
            "status": "ignored",
            "message": f"Run {run_id} is not currently running or queued (status: {run.get('status')})",
            "run_id": run_id,
        })

    from src import job_control

    job_control.request_stop_run(run_id)
    store.stop_run(run_id, reason="Stopped by user from dashboard")

    topic = str(run.get("topic") or "")
    fmt = str(run.get("format") or "short")
    key = _job_key(topic, fmt)
    with _running_lock:
        _running_jobs.discard(key)

    return JSONResponse({"status": "stopped", "run_id": run_id, "topic": topic})


@app.post("/api/stop-all")
def api_stop_all() -> JSONResponse:
    from src import job_control

    stopped_ids = job_control.request_stop_all()
    for rid in stopped_ids:
        store.stop_run(rid, reason="Stopped all runs by user")

    with store.db() as conn:
        rows = conn.execute("SELECT id FROM runs WHERE status IN ('running', 'queued')").fetchall()
        for row in rows:
            rid = int(row["id"])
            if rid not in stopped_ids:
                store.stop_run(rid, reason="Stopped all runs by user")

    with _running_lock:
        _running_jobs.clear()

    job_control.reset_stop_all()
    return JSONResponse({"status": "stopped_all", "stopped_run_ids": stopped_ids})


@app.post("/api/runs/{run_id}/retry")
async def api_retry_run(
    run_id: int,
    background_tasks: BackgroundTasks,
    mock: bool = False,
    force_upload: bool = False,
) -> JSONResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("status") in ("running", "queued"):
        raise HTTPException(status_code=409, detail=f"Run {run_id} is already running or queued")

    topic = str(run.get("topic") or "").strip()
    fmt = str(run.get("format") or "short").strip()
    key = _job_key(topic, fmt)

    from src import job_control

    job_control.clear_stop(run_id, topic)

    with _running_lock:
        if key in _running_jobs:
            raise HTTPException(status_code=409, detail=f"Topic '{topic}' is already running or queued")
        _running_jobs.add(key)

    store.queue_run_for_retry(run_id)

    def _bg_retry() -> None:
        from src.job_control import JobStoppedError, check_stop
        from src.pipeline import retry_single_topic

        try:
            check_stop(run_id, topic)
            with _generate_semaphore:
                check_stop(run_id, topic)
                retry_single_topic(
                    run_id,
                    mock=mock,
                    skip_upload=not _youtube_enabled(),
                    force_upload=force_upload,
                )
        except JobStoppedError:
            logger.info("Queued retry %s for %s stopped by user", run_id, topic)
            store.stop_run(run_id, reason="Stopped by user")
        except Exception:
            logger.exception("Retry failed for run %s", run_id)
        finally:
            with _running_lock:
                _running_jobs.discard(key)

    background_tasks.add_task(_bg_retry)
    return JSONResponse({"status": "retry_queued", "run_id": run_id, "topic": topic})


@app.post("/api/retry-failed")
def api_retry_failed(
    background_tasks: BackgroundTasks,
    mock: bool = False,
) -> JSONResponse:
    failed_runs = store.list_failed_runs()
    if not failed_runs:
        return JSONResponse({"status": "none", "message": "No failed or stopped runs found to retry"})

    runs_to_retry = [(int(r["id"]), str(r.get("topic") or ""), str(r.get("format") or "short")) for r in failed_runs]

    from src import job_control

    for rid, topic, fmt in runs_to_retry:
        job_control.clear_stop(rid, topic)
        store.queue_run_for_retry(rid)

    def _bg_batch_retry() -> None:
        from src.job_control import JobStoppedError, check_stop
        from src.pipeline import retry_single_topic

        def _retry_one(rid: int, topic: str, fmt: str) -> None:
            key = _job_key(topic, fmt)
            try:
                check_stop(rid, topic)
            except JobStoppedError:
                logger.info("Batch retry stopped by user for run %s (%r)", rid, topic)
                store.stop_run(rid, reason="Stopped by user while queued")
                return

            curr = store.get_run(rid)
            if curr and curr.get("status") == "stopped":
                logger.info("Retry run %s was stopped while queued; skipping", rid)
                return

            with _generate_semaphore:
                try:
                    check_stop(rid, topic)
                    with _running_lock:
                        if key in _running_jobs:
                            return
                        _running_jobs.add(key)
                    retry_single_topic(
                        rid,
                        mock=mock,
                        skip_upload=not _youtube_enabled(),
                    )
                except JobStoppedError:
                    logger.info("Queued retry %s for %s stopped by user", rid, topic)
                    store.stop_run(rid, reason="Stopped by user")
                except Exception:
                    logger.exception("Batch retry failed for run %s", rid)
                finally:
                    with _running_lock:
                        _running_jobs.discard(key)

        effective_limit = _generate_semaphore.limit
        if effective_limit > 1 and len(runs_to_retry) > 1:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=effective_limit, thread_name_prefix="retry_failed"
            ) as executor:
                futures = [
                    executor.submit(_retry_one, rid, topic, fmt)
                    for rid, topic, fmt in runs_to_retry
                ]
                concurrent.futures.wait(futures)
        else:
            for rid, topic, fmt in runs_to_retry:
                _retry_one(rid, topic, fmt)

    background_tasks.add_task(_bg_batch_retry)
    return JSONResponse({
        "status": "retry_queued",
        "retrying_run_ids": [r[0] for r in runs_to_retry],
    })



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

    topic = str(run.get("topic") or "").strip()
    wiki_title = str(run.get("wiki_title") or "").strip()
    if (
        run.get("upload_status") == "uploaded"
        and run.get("youtube_video_id")
        and run.get("youtube_video_id") != "skipped"
    ):
        raise HTTPException(
            status_code=409,
            detail=f"This video has already been uploaded to YouTube (ID: {run.get('youtube_video_id')}).",
        )

    if (
        store.is_topic_uploaded(topic, exclude_run_id=run_id)
        or (wiki_title and store.is_topic_uploaded(wiki_title, exclude_run_id=run_id))
    ):
        raise HTTPException(
            status_code=409,
            detail=f"Topic '{topic}' has already been uploaded to YouTube in another run.",
        )

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

    if not mock and store.is_topic_covered(topic):
        if store.is_topic_uploaded(topic):
            detail_msg = f"Topic '{topic}' has already been uploaded to YouTube."
        else:
            detail_msg = f"Topic '{topic}' is already covered (video already generated or currently in progress)."
        raise HTTPException(status_code=409, detail=detail_msg)

    key = _job_key(topic, fmt)
    with _running_lock:
        if key in _running_jobs:
            raise HTTPException(status_code=409, detail="That topic is already generating or queued")
        _running_jobs.add(key)

    run_date = local_run_date()
    rid = store.create_run(topic, fmt, run_date, status="queued")
    store.append_step_log(rid, "queued", f"Generation queued for {topic}")

    def _bg() -> None:
        from src.job_control import JobStoppedError, check_stop
        from src.pipeline import run_topic

        try:
            check_stop(rid, topic)
            with _generate_semaphore:
                check_stop(rid, topic)
                run_topic(
                    topic,
                    fmt,
                    skip_upload=not _youtube_enabled(),
                    mock=mock,
                    existing_run_id=rid,
                )
        except JobStoppedError:
            logger.info("Job %s for %s stopped by user", rid, topic)
            store.stop_run(rid, reason="Stopped by user")
        except Exception:
            logger.exception("Background generate failed for %s %s", fmt, topic)
        finally:
            with _running_lock:
                _running_jobs.discard(key)

    background_tasks.add_task(_bg)
    return JSONResponse({"status": "queued", "run_id": rid, "topic": topic, "format": fmt})


@app.get("/api/concurrency")
async def api_get_concurrency() -> JSONResponse:
    state = load_pipeline_concurrency()
    return JSONResponse(
        {
            "ok": True,
            "status": "ok",
            "enabled": state["concurrency_enabled"],
            "max_parallel_jobs": state["max_parallel_jobs"],
            "effective_limit": _generate_semaphore.limit,
            "active_jobs": _generate_semaphore.active_count,
        }
    )


@app.post("/api/concurrency")
async def api_set_concurrency(request: Request) -> JSONResponse:
    payload: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    else:
        try:
            form = await request.form()
            for k, v in form.items():
                payload[k] = v
        except Exception:
            payload = {}

    enabled: bool | None = None
    if "enabled" in payload:
        val = payload["enabled"]
        if isinstance(val, bool):
            enabled = val
        elif isinstance(val, str):
            enabled = val.strip().lower() in ("true", "1", "on", "yes")

    max_parallel_jobs: int | None = None
    if "max_parallel_jobs" in payload:
        try:
            max_parallel_jobs = int(payload["max_parallel_jobs"])
        except (ValueError, TypeError):
            pass

    updated = update_pipeline_concurrency(
        enabled=enabled,
        max_parallel=max_parallel_jobs,
    )
    _generate_semaphore.set_limit(updated["effective_limit"])

    return JSONResponse(
        {
            "ok": True,
            "status": "ok",
            "message": "Concurrency settings updated",
            "enabled": updated["concurrency_enabled"],
            "max_parallel_jobs": updated["max_parallel_jobs"],
            "effective_limit": updated["effective_limit"],
            "active_jobs": _generate_semaphore.active_count,
        }
    )


@app.get("/api/settings/delete-after-upload")
async def api_get_delete_after_upload() -> JSONResponse:
    return JSONResponse({"ok": True, "enabled": should_delete_after_upload()})


@app.post("/api/settings/delete-after-upload")
async def api_set_delete_after_upload(request: Request) -> JSONResponse:
    payload: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    else:
        try:
            form = await request.form()
            for k, v in form.items():
                payload[k] = v
        except Exception:
            payload = {}

    enabled = True
    if "enabled" in payload:
        val = payload["enabled"]
        if isinstance(val, bool):
            enabled = val
        elif isinstance(val, str):
            enabled = val.strip().lower() in ("true", "1", "on", "yes")

    updated = update_delete_after_upload(enabled)
    return JSONResponse({"ok": True, "enabled": updated})


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
