"""Download freely licensed Wikipedia / Commons stills for a topic."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from src.config import load_pipeline_config
from src.wiki.fetcher import USER_AGENT, WIKI_API

logger = logging.getLogger(__name__)

_LOW_VALUE_RE = re.compile(
    r"(?:^|/)(?:logo|icon|sprite|favicon|badge|wordmark)(?:[._/-]|$)|"
    r"\.(?:svg)(?:\?|$)|"
    r"flag_of_|coat_of_arms|padlock|question_book|edit[-_]?icon|"
    r"commons-logo|wikimedia-logo|wiki_letter",
    re.IGNORECASE,
)
_FAIR_USE_RE = re.compile(
    r"fair[\s-]?use|non[- ]?free|copyrighted|all rights reserved",
    re.IGNORECASE,
)
_FREE_HINT_RE = re.compile(
    r"public domain|cc[- ]?by|creative commons|gfdl|cc0",
    re.IGNORECASE,
)


def _is_low_value(name: str, url: str = "") -> bool:
    blob = f"{name} {url}"
    return bool(_LOW_VALUE_RE.search(blob))


def _license_ok(meta: dict[str, Any], skip_fair_use: bool) -> bool:
    usage = str(meta.get("UsageTerms", {}).get("value") or "")
    short = str(meta.get("LicenseShortName", {}).get("value") or "")
    license_url = str(meta.get("LicenseUrl", {}).get("value") or "")
    blob = f"{usage} {short} {license_url}"
    if skip_fair_use and _FAIR_USE_RE.search(blob):
        return False
    if "nonfree" in blob.lower() or "non-free" in blob.lower():
        return False
    if _FREE_HINT_RE.search(blob) or not blob.strip():
        return True
    # Unknown license: skip to stay safe
    if skip_fair_use and blob.strip() and not _FREE_HINT_RE.search(blob):
        return False
    return True


def _download(client: httpx.Client, url: str, dest: Path) -> bool:
    try:
        resp = client.get(url, follow_redirects=True, timeout=25.0)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "image" not in content_type and not url.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        ):
            return False
        data = resp.content
        if len(data) < 2500:
            return False
        dest.write_bytes(data)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Image download failed %s: %s", url, exc)
        return False


def _page_image_infos(
    client: httpx.Client, title: str, limit: int
) -> list[dict[str, Any]]:
    resp = client.get(
        WIKI_API,
        params={
            "action": "query",
            "generator": "images",
            "titles": title,
            "gimlimit": str(max(limit * 3, 8)),
            "prop": "imageinfo",
            "iiprop": "url|mime|size|extmetadata",
            "iiurlwidth": "1280",
            "format": "json",
            "formatversion": "2",
        },
        timeout=30.0,
    )
    if resp.status_code == 403:
        logger.warning("Wikipedia image query 403 for %s", title)
        return []
    resp.raise_for_status()
    pages = (resp.json().get("query") or {}).get("pages") or []
    infos: list[dict[str, Any]] = []
    for page in pages:
        for info in page.get("imageinfo") or []:
            infos.append(
                {
                    "title": str(page.get("title") or ""),
                    **info,
                }
            )
    return infos


def fetch_article_images(
    article: dict[str, Any],
    output_dir: Path,
    *,
    mock: bool = False,
) -> list[dict[str, Any]]:
    """
    Download free stills. Returns credit records with local `path`.
    """
    config = load_pipeline_config()
    img_cfg = config.get("images") or {}
    max_images = int(img_cfg.get("max_per_article") or 8)
    skip_fair_use = bool(img_cfg.get("skip_fair_use", True))
    dest_dir = output_dir / "images"
    dest_dir.mkdir(parents=True, exist_ok=True)

    if mock:
        from PIL import Image, ImageDraw, ImageFont

        credits: list[dict[str, Any]] = []
        for i in range(3):
            path = dest_dir / f"mock_{i + 1}.png"
            img = Image.new("RGB", (1280, 720), (18, 32, 56))
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 48)
            except OSError:
                font = ImageFont.load_default()
            draw.text((40, 320), f"{article.get('title') or 'Topic'} · {i + 1}", fill=(230, 240, 255), font=font)
            img.save(path)
            credits.append(
                {
                    "path": str(path),
                    "title": "Mock placeholder",
                    "artist": "Wiki Video Pipeline",
                    "license": "Public domain",
                    "source_url": "",
                    "file_url": "",
                }
            )
        return credits

    headers = {"User-Agent": USER_AGENT, "Api-User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    credits: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
        thumb = str(article.get("thumbnail") or "").strip()
        if thumb and not _is_low_value("lead", thumb):
            dest = dest_dir / "00_lead.jpg"
            if _download(client, thumb, dest):
                seen_urls.add(thumb.split("?")[0])
                credits.append(
                    {
                        "path": str(dest),
                        "title": str(article.get("title") or "Lead image"),
                        "artist": "Wikipedia / Wikimedia contributors",
                        "license": "see file page",
                        "source_url": str(article.get("url") or ""),
                        "file_url": thumb,
                    }
                )

        try:
            infos = _page_image_infos(
                client, str(article.get("title") or ""), max_images
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Page images failed: %s", exc)
            infos = []

        for info in infos:
            if len(credits) >= max_images:
                break
            mime = str(info.get("mime") or "")
            if not mime.startswith("image/") or "svg" in mime:
                continue
            url = str(info.get("thumburl") or info.get("url") or "")
            if not url or url.split("?")[0] in seen_urls:
                continue
            title = str(info.get("title") or "")
            if _is_low_value(title, url):
                continue
            meta = info.get("extmetadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            if not _license_ok(meta, skip_fair_use):
                logger.info("Skip non-free image %s", title)
                continue
            dest = dest_dir / f"{len(credits):02d}_{re.sub(r'[^\w]+', '_', title)[:40]}.jpg"
            if not _download(client, url, dest):
                continue
            seen_urls.add(url.split("?")[0])
            artist = str(meta.get("Artist", {}).get("value") or "Unknown")
            artist = re.sub(r"<[^>]+>", "", artist).strip() or "Unknown"
            license_name = str(
                meta.get("LicenseShortName", {}).get("value")
                or meta.get("UsageTerms", {}).get("value")
                or "see file page"
            )
            description_url = str(
                meta.get("LicenseUrl", {}).get("value")
                or info.get("descriptionurl")
                or ""
            )
            file_page = str(info.get("descriptionurl") or "")
            if not file_page and title:
                file_page = f"https://commons.wikimedia.org/wiki/{quote(title)}"
            credits.append(
                {
                    "path": str(dest),
                    "title": title,
                    "artist": artist[:200],
                    "license": license_name,
                    "source_url": file_page,
                    "file_url": url,
                    "license_url": description_url,
                }
            )

    logger.info("Downloaded %s images for %s", len(credits), article.get("title"))
    return credits
