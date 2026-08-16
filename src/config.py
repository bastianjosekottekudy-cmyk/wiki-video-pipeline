"""Project paths and configuration loading."""

from __future__ import annotations

import os
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


def load_pipeline_config() -> dict[str, Any]:
    path = CONFIG_DIR / "pipeline.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


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
