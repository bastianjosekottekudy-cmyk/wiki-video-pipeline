"""Project paths and configuration loading."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
OUTPUT_DIR = PROJECT_ROOT / "output"
SECRETS_DIR = PROJECT_ROOT / "secrets"
TIMEZONE = "Asia/Kolkata"

# override=True so .env wins over stale shell vars (e.g. SKIP_YOUTUBE_UPLOAD)
load_dotenv(PROJECT_ROOT / ".env", override=True)


DEFAULT_SCHEDULE_HOUR = 20
DEFAULT_SCHEDULE_MINUTE = 0
DEFAULT_SCHEDULE_TIMEZONE = TIMEZONE
DEFAULT_DAILY_TOPICS_COUNT = 1
DEFAULT_MAX_CONCURRENT_JOBS = 5


def load_pipeline_config() -> dict[str, Any]:
    path = CONFIG_DIR / "pipeline.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_pipeline_config(data: dict[str, Any]) -> None:
    path = CONFIG_DIR / "pipeline.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def load_schedule_config() -> dict[str, Any]:
    cfg = load_pipeline_config().get("schedule") or {}
    hour = int(cfg.get("hour", DEFAULT_SCHEDULE_HOUR))
    minute = int(cfg.get("minute", DEFAULT_SCHEDULE_MINUTE))
    daily_count = int(cfg.get("daily_topics_count", DEFAULT_DAILY_TOPICS_COUNT))
    enabled = bool(cfg.get("enabled", True))
    auto_upload = bool(cfg.get("auto_upload", True))
    tz = str(cfg.get("timezone", DEFAULT_SCHEDULE_TIMEZONE))
    return {
        "enabled": enabled,
        "hour": min(max(hour, 0), 23),
        "minute": min(max(minute, 0), 59),
        "daily_topics_count": min(max(daily_count, 1), 20),
        "auto_upload": auto_upload,
        "timezone": tz,
        "time_ist": f"{hour:02d}:{minute:02d}",
    }


def update_schedule_config(
    *,
    enabled: bool | None = None,
    hour: int | None = None,
    minute: int | None = None,
    daily_topics_count: int | None = None,
    auto_upload: bool | None = None,
) -> dict[str, Any]:
    full_cfg = load_pipeline_config()
    sched = dict(full_cfg.get("schedule") or {})
    if enabled is not None:
        sched["enabled"] = bool(enabled)
    if hour is not None:
        sched["hour"] = min(max(int(hour), 0), 23)
    if minute is not None:
        sched["minute"] = min(max(int(minute), 0), 59)
    if daily_topics_count is not None:
        sched["daily_topics_count"] = min(max(int(daily_topics_count), 1), 20)
    if auto_upload is not None:
        sched["auto_upload"] = bool(auto_upload)
    sched.setdefault("timezone", DEFAULT_SCHEDULE_TIMEZONE)
    full_cfg["schedule"] = sched
    save_pipeline_config(full_cfg)
    return load_schedule_config()


def load_topics_config() -> dict[str, Any]:
    cfg = load_pipeline_config().get("topics") or {}
    pool = list(cfg.get("pool") or [])
    categories = list(cfg.get("categories") or [])
    return {
        "pool": [str(t).strip() for t in pool if str(t).strip()],
        "categories": [str(c).strip() for c in categories if str(c).strip()],
    }


def update_topics_pool(pool: list[str]) -> list[str]:
    full_cfg = load_pipeline_config()
    topics = dict(full_cfg.get("topics") or {})
    cleaned = []
    seen = set()
    for t in pool:
        s = str(t).strip()
        if s and s.lower() not in seen:
            cleaned.append(s)
            seen.add(s.lower())
    topics["pool"] = cleaned
    full_cfg["topics"] = topics
    save_pipeline_config(full_cfg)
    return cleaned


def add_topic_to_pool(topic: str) -> list[str]:
    curr = load_topics_config()["pool"]
    s = topic.strip()
    if s and not any(s.lower() == x.lower() for x in curr):
        curr.append(s)
        return update_topics_pool(curr)
    return curr


def get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def load_pipeline_concurrency() -> dict[str, Any]:
    cfg = load_pipeline_config()
    enabled = bool(cfg.get("concurrency_enabled", True))
    max_parallel = max(1, int(cfg.get("max_parallel_jobs", DEFAULT_MAX_CONCURRENT_JOBS)))
    return {
        "concurrency_enabled": enabled,
        "max_parallel_jobs": max_parallel,
        "effective_limit": max_parallel if enabled else 1,
    }


def update_pipeline_concurrency(
    *,
    enabled: bool | None = None,
    max_parallel: int | None = None,
) -> dict[str, Any]:
    path = CONFIG_DIR / "pipeline.yaml"
    text = path.read_text(encoding="utf-8")

    if enabled is not None:
        val_str = "true" if enabled else "false"
        if re.search(r"^concurrency_enabled\s*:.*$", text, flags=re.MULTILINE):
            text = re.sub(
                r"^concurrency_enabled\s*:.*$",
                f"concurrency_enabled: {val_str}",
                text,
                flags=re.MULTILINE,
            )
        else:
            if re.search(r"^max_parallel_jobs\s*:.*$", text, flags=re.MULTILINE):
                text = re.sub(
                    r"^(max_parallel_jobs\s*:.*)$",
                    rf"\1\nconcurrency_enabled: {val_str}",
                    text,
                    flags=re.MULTILINE,
                )
            else:
                text = f"concurrency_enabled: {val_str}\n" + text

    if max_parallel is not None:
        clamped = min(max(int(max_parallel), 1), 20)
        if re.search(r"^max_parallel_jobs\s*:.*$", text, flags=re.MULTILINE):
            text = re.sub(
                r"^max_parallel_jobs\s*:.*$",
                f"max_parallel_jobs: {clamped}",
                text,
                flags=re.MULTILINE,
            )
        else:
            text = f"max_parallel_jobs: {clamped}\n" + text

    path.write_text(text, encoding="utf-8")
    return load_pipeline_concurrency()


def load_execution_config() -> dict[str, Any]:
    conc = load_pipeline_concurrency()
    return {
        "max_concurrent_jobs": conc["effective_limit"],
        "concurrency_enabled": conc["concurrency_enabled"],
        "max_parallel_jobs": conc["max_parallel_jobs"],
    }


def local_run_date() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def format_profile(fmt: str) -> dict[str, Any]:
    """Resolution / duration profile for short vs video."""
    key = (fmt or "short").strip().lower()
    if key not in ("short", "video"):
        raise ValueError(f"Unknown format: {fmt!r} (use short or video)")
    config = load_pipeline_config()
    profiles = config.get("formats") or {}
    profile = dict(profiles.get(key) or {})
    if key == "short":
        profile.setdefault("width", 1080)
        profile.setdefault("height", 1920)
        profile.setdefault("max_video_duration_sec", 180)
        profile.setdefault("target_duration_sec", 120)
        profile.setdefault("source_chars", 8000)
    else:
        profile.setdefault("width", 1920)
        profile.setdefault("height", 1080)
        profile.setdefault("max_video_duration_sec", 0)
        profile.setdefault("target_duration_sec", 0)
        profile.setdefault("source_chars", 18000)
    return profile


def run_output_dir(run_date: str, fmt: str, run_id: int | None = None) -> Path:
    path = OUTPUT_DIR / run_date / fmt.lower()
    if run_id is not None:
        path = path / f"run_{run_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path
