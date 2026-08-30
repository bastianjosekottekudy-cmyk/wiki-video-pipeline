"""YouTube title and filename helpers."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def format_display_date(run_date: str) -> str:
    try:
        dt = datetime.strptime(run_date, "%Y-%m-%d")
        return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    except ValueError:
        return run_date


def sanitize_title(title: str, max_len: int = 100) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "").strip())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned or "Wikipedia"


def build_video_title(wiki_title: str, fmt: str, run_date: str) -> str:
    """YouTube / filename title, max 100 chars."""
    name = sanitize_title(wiki_title, max_len=70)
    if (fmt or "").lower() == "short":
        title = f"{name} in a minute"
    else:
        title = f"{name}: the story"
    if len(title) > 100:
        title = title[:99].rstrip() + "…"
    return title


def safe_filename(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip()
    return cleaned or "wiki-video"


def video_filename(wiki_title: str, fmt: str, run_date: str) -> str:
    return f"{safe_filename(build_video_title(wiki_title, fmt, run_date))}.mp4"


def title_from_video_path(
    video_path: str | None,
    wiki_title: str = "",
    fmt: str = "",
    run_date: str = "",
) -> str:
    if video_path:
        stem = Path(video_path).stem
        if stem and stem != "final":
            return stem
    if wiki_title:
        return build_video_title(wiki_title, fmt, run_date)
    return wiki_title or "Wikipedia video"
