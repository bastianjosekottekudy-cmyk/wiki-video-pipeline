"""YouTube upload — multi-client OAuth failover, CC BY-SA attribution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from src.config import get_env, load_pipeline_config
from src.naming import build_video_title
from src.youtube.auth import (
    YouTubeClient,
    get_credentials_for_client,
    list_youtube_clients,
)

logger = logging.getLogger(__name__)

CC_BY_SA = "https://creativecommons.org/licenses/by-sa/4.0/"


class YouTubeUploadError(RuntimeError):
    """Raised when an upload cannot proceed or fails."""


def _build_description(
    article: dict[str, Any],
    credits: list[dict[str, Any]],
    fmt: str,
    video_title: str,
) -> str:
    wiki_title = str(article.get("title") or article.get("topic") or "Wikipedia")
    wiki_url = str(article.get("url") or "")
    lines = [
        video_title,
        "",
        f"Narration adapted from Wikipedia article “{wiki_title}”.",
        wiki_url,
        "",
        "This video uses material from Wikipedia, which is released under the",
        f"Creative Commons Attribution-ShareAlike License 4.0: {CC_BY_SA}",
        "Changes: spoken documentary narration and on-screen slides.",
        "",
    ]
    if credits:
        lines.append("Images:")
        for credit in credits:
            artist = str(credit.get("artist") or "Unknown")
            license_name = str(credit.get("license") or "see file page")
            source = str(credit.get("source_url") or credit.get("file_url") or "")
            title = str(credit.get("title") or "image")
            lines.append(f"- {title} — {artist} — {license_name}")
            if source:
                lines.append(f"  {source}")
        lines.append("")
    tags = ["#Wikipedia", "#Education", "#Explainer"]
    if fmt == "short":
        tags.insert(0, "#Shorts")
    lines.extend(tags)
    return "\n".join(lines)


def youtube_enabled() -> bool:
    config = load_pipeline_config()
    return bool(config.get("youtube", {}).get("enabled", False))


def is_retryable_upload_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    needles = (
        "429",
        "quota exceeded",
        "ratelimitexceeded",
        "rate limit",
        "uploadlimitexceeded",
        "invalid_grant",
        "expired or revoked",
        "auth failed",
        "refresherror",
        "credentials",
        "token",
        "oauth",
        "connection",
        "timeout",
        "timed out",
        "max retries exceeded",
        "temporarily unavailable",
        "backenderror",
        "internalerror",
        "503",
        "500",
    )
    return any(n in text for n in needles)


def _upload_with_client(
    client: YouTubeClient,
    path: Path,
    article: dict[str, Any],
    credits: list[dict[str, Any]],
    fmt: str,
    run_date: str,
    yt_cfg: dict[str, Any],
) -> str:
    try:
        creds = get_credentials_for_client(client, allow_browser=False)
    except Exception as exc:
        raise YouTubeUploadError(
            f"YouTube auth failed for client {client.id}. "
            f"Run: python -m src.youtube.auth --client {client.id} ({exc})"
        ) from exc

    youtube = build("youtube", "v3", credentials=creds)
    wiki_title = str(article.get("title") or article.get("topic") or "Wikipedia")
    title = build_video_title(wiki_title, fmt, run_date)
    description = _build_description(article, credits, fmt, title)
    tags = list(yt_cfg.get("tags") or ["wikipedia", "education", "explainer"])
    if fmt == "short":
        tags = ["shorts", *tags]

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": str(yt_cfg.get("category_id", "27")),
        },
        "status": {
            "privacyStatus": yt_cfg.get("privacy", "public"),
            "selfDeclaredMadeForKids": bool(yt_cfg.get("made_for_kids", False)),
            "license": str(yt_cfg.get("license") or "creativeCommon"),
        },
    }

    media = MediaFileUpload(str(path), chunksize=256 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info(
                    "Upload progress (%s): %.1f%%",
                    client.id,
                    status.progress() * 100,
                )
    except Exception as exc:
        raise YouTubeUploadError(
            f"YouTube API upload failed ({client.id}): {exc}"
        ) from exc

    if not response or not response.get("id"):
        raise YouTubeUploadError(f"YouTube API returned no video id ({client.id})")

    video_id = response["id"]
    logger.info(
        "Uploaded via %s: https://youtube.com/watch?v=%s",
        client.id,
        video_id,
    )
    return video_id


def upload_video(
    video_path: str,
    article: dict[str, Any],
    credits: list[dict[str, Any]],
    fmt: str,
    run_date: str,
) -> str:
    skip = get_env("SKIP_YOUTUBE_UPLOAD", "false").strip().lower()
    if skip in ("true", "1", "yes"):
        raise YouTubeUploadError(
            "YouTube upload skipped (SKIP_YOUTUBE_UPLOAD=true). "
            "Set SKIP_YOUTUBE_UPLOAD=false in .env and restart the app."
        )

    if not youtube_enabled():
        raise YouTubeUploadError(
            "YouTube upload is disabled (set youtube.enabled: true in config/pipeline.yaml)"
        )

    path = Path(video_path)
    if not path.is_file():
        raise YouTubeUploadError(f"Video file not found: {video_path}")

    config = load_pipeline_config()
    yt_cfg = config.get("youtube", {})
    clients = list_youtube_clients()
    if not clients:
        raise YouTubeUploadError("No YouTube OAuth clients configured")

    errors: list[str] = []
    for i, client in enumerate(clients):
        try:
            logger.info(
                "YouTube upload attempt via client %s (%s/%s)",
                client.id,
                i + 1,
                len(clients),
            )
            return _upload_with_client(
                client, path, article, credits, fmt, run_date, yt_cfg
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            errors.append(f"{client.id}: {msg}")
            has_next = i + 1 < len(clients)
            if has_next and is_retryable_upload_error(exc):
                logger.warning(
                    "YouTube client %s failed (retryable) — trying next: %s",
                    client.id,
                    msg[:240],
                )
                continue
            if has_next:
                logger.error(
                    "YouTube client %s failed (non-retryable) — stopping: %s",
                    client.id,
                    msg[:240],
                )
                raise YouTubeUploadError(msg) from exc
            logger.error(
                "YouTube client %s failed (last in chain): %s",
                client.id,
                msg[:240],
            )

    summary = " | ".join(errors) if errors else "unknown error"
    raise YouTubeUploadError(
        f"All YouTube OAuth clients failed ({len(clients)}): {summary}"
    )
