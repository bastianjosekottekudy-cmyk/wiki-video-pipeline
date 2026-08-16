"""Resolve a topic to an English Wikipedia article and extract sectioned text."""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from src.config import format_profile

logger = logging.getLogger(__name__)

# Wikimedia requires a contact URL in the UA; strings without one get HTTP 403.
USER_AGENT = (
    "WikiVideoPipeline/1.0 "
    "(https://en.wikipedia.org/wiki/Wikipedia:Reusing_Wikipedia_content; "
    "local educational tool; operator Bastian) python-httpx"
)
WIKI_REST = "https://en.wikipedia.org/w/rest.php/v1"
WIKI_API = "https://en.wikipedia.org/w/api.php"
PCS_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary"

SKIP_SECTIONS = {
    "references",
    "notes",
    "see also",
    "external links",
    "bibliography",
    "further reading",
    "citations",
    "sources",
    "notes and references",
    "footnotes",
    "works cited",
    "publications",
    "gallery",
}

SKIP_SUMMARY_TYPES = {"disambiguation", "mainpage", "no-extract"}

_HEADING_RE = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NL_RE = re.compile(r"\n{3,}")


class WikiFetchError(RuntimeError):
    """Raised when a topic cannot be resolved to a usable article."""


def _client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Api-User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
        follow_redirects=True,
        timeout=30.0,
    )


def _raise_wiki_http(resp: httpx.Response, what: str) -> None:
    if resp.status_code == 403:
        raise WikiFetchError(
            f"Wikimedia blocked {what} (HTTP 403 robot policy). "
            "Use an identifying User-Agent with a contact URL, keep requests serial, "
            "or use --mock. Contact bot-traffic@wikimedia.org if it persists."
        )
    resp.raise_for_status()


def _title_key(title: str) -> str:
    return (title or "").strip().replace(" ", "_")


def _search_pages_action(client: httpx.Client, topic: str, limit: int) -> list[dict[str, Any]]:
    resp = client.get(
        WIKI_API,
        params={
            "action": "query",
            "list": "search",
            "srsearch": topic,
            "srlimit": str(limit),
            "srnamespace": "0",
            "format": "json",
            "formatversion": "2",
        },
    )
    _raise_wiki_http(resp, "Action API search")
    hits = (resp.json().get("query") or {}).get("search") or []
    out: list[dict[str, Any]] = []
    for hit in hits:
        title = str(hit.get("title") or "").strip()
        if title:
            out.append({"title": title, "key": _title_key(title), "description": ""})
    return out


def _search_pages(client: httpx.Client, topic: str, limit: int = 5) -> list[dict[str, Any]]:
    resp = client.get(
        f"{WIKI_REST}/search/page",
        params={"q": topic, "limit": str(limit)},
    )
    if resp.status_code == 403:
        logger.warning("REST search 403 — trying Action API")
        return _search_pages_action(client, topic, limit)
    _raise_wiki_http(resp, "REST search")
    pages = resp.json().get("pages") or []
    out: list[dict[str, Any]] = []
    for page in pages:
        title = str(page.get("title") or page.get("key") or "").strip()
        if title:
            out.append(
                {
                    "title": title,
                    "key": str(page.get("key") or _title_key(title)),
                    "description": str(page.get("description") or ""),
                }
            )
    return out or _search_pages_action(client, topic, limit)


def _summary(client: httpx.Client, title: str) -> dict[str, Any] | None:
    resp = client.get(f"{PCS_SUMMARY}/{quote(_title_key(title), safe='')}")
    if resp.status_code == 404:
        return None
    _raise_wiki_http(resp, "page summary")
    return resp.json()


def _extracts_wiki(client: httpx.Client, title: str) -> str:
    resp = client.get(
        WIKI_API,
        params={
            "action": "query",
            "prop": "extracts",
            "explaintext": "1",
            "exsectionformat": "wiki",
            "titles": title,
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
        },
    )
    _raise_wiki_http(resp, "TextExtracts")
    pages = (resp.json().get("query") or {}).get("pages") or []
    if not pages:
        return ""
    return str(pages[0].get("extract") or "").strip()


def _html_to_wiki_sections(html_text: str) -> str:
    text = html_text or ""
    text = re.sub(
        r"<h([1-6])[^>]*>(.*?)</h\1>",
        lambda m: "\n" + ("=" * (int(m.group(1)) + 1)) + " "
        + _HTML_TAG_RE.sub("", m.group(2)).strip()
        + " "
        + ("=" * (int(m.group(1)) + 1))
        + "\n",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    return _MULTI_NL_RE.sub("\n\n", text).strip()


def _extracts_html_fallback(client: httpx.Client, title: str) -> str:
    resp = client.get(f"{WIKI_REST}/page/{quote(_title_key(title), safe='')}/html")
    if resp.status_code == 404:
        return ""
    resp.raise_for_status()
    return _html_to_wiki_sections(resp.text)


def parse_sections(extract: str) -> tuple[str, list[dict[str, str]]]:
    """Split wiki-formatted extract into lead + named sections."""
    lines = (extract or "").splitlines()
    lead_lines: list[str] = []
    sections: list[dict[str, str]] = []
    current_title = ""
    current_body: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_body
        if not current_title:
            return
        body = "\n".join(current_body).strip()
        if body:
            sections.append({"title": current_title, "text": body})
        current_title = ""
        current_body = []

    in_lead = True
    for line in lines:
        match = _HEADING_RE.match(line.strip())
        if match:
            heading = match.group(2).strip()
            in_lead = False
            flush()
            current_title = heading
            continue
        if in_lead:
            lead_lines.append(line)
        else:
            current_body.append(line)
    flush()
    lead = "\n".join(lead_lines).strip()
    return lead, sections


def _keep_section(title: str) -> bool:
    return title.strip().lower() not in SKIP_SECTIONS


def _cap_source(lead: str, sections: list[dict[str, str]], max_chars: int) -> str:
    parts: list[str] = []
    if lead:
        parts.append(lead)
    used = len(lead)
    kept: list[dict[str, str]] = []
    for section in sections:
        if not _keep_section(section["title"]):
            continue
        chunk = f"\n\n== {section['title']} ==\n{section['text']}"
        if used + len(chunk) > max_chars and kept:
            break
        if used + len(chunk) > max_chars:
            remain = max(0, max_chars - used - len(section["title"]) - 10)
            trimmed = section["text"][:remain].rsplit(" ", 1)[0]
            if trimmed:
                kept.append({"title": section["title"], "text": trimmed})
            break
        kept.append(section)
        used += len(chunk)
        parts.append(f"== {section['title']} ==\n{section['text']}")
    return "\n\n".join(parts).strip()


def _mock_article(topic: str, fmt: str) -> dict[str, Any]:
    title = topic.strip() or "Mock topic"
    lead = (
        f"{title} is a mock Wikipedia article used when --mock is set. "
        "It exists so the pipeline can render a video without calling Wikimedia."
    )
    sections = [
        {
            "title": "What it is",
            "text": (
                f"{title} is often remembered for one surprising detail: "
                "the story is more interesting than a dry definition. "
                "People study it because it connects everyday curiosity with a bigger idea."
            ),
        },
        {
            "title": "Why it matters",
            "text": (
                "The useful part is not a list of dates. It is the turning point — "
                "the moment something ordinary became extraordinary, and why that still matters."
            ),
        },
    ]
    source = _cap_source(lead, sections, format_profile(fmt)["source_chars"])
    return {
        "topic": topic,
        "title": title,
        "key": _title_key(title),
        "url": f"https://en.wikipedia.org/wiki/{quote(_title_key(title))}",
        "description": "Mock article for local smoke tests",
        "extract_type": "mock",
        "thumbnail": "",
        "lead": lead,
        "sections": sections,
        "source_text": source,
        "mock": True,
    }


def fetch_article(topic: str, fmt: str = "video") -> dict[str, Any]:
    """Resolve topic → canonical English Wikipedia article + capped section text."""
    topic = (topic or "").strip()
    if not topic:
        raise WikiFetchError("Topic is required")

    profile = format_profile(fmt)
    max_chars = int(profile.get("source_chars") or 12000)

    with _client() as client:
        hits = _search_pages(client, topic)
        if not hits:
            raise WikiFetchError(f"No Wikipedia results for {topic!r}")

        chosen: dict[str, Any] | None = None
        summary: dict[str, Any] | None = None
        for hit in hits:
            summary = _summary(client, hit["title"])
            if not summary:
                continue
            kind = str(summary.get("type") or "standard").lower()
            if kind in SKIP_SUMMARY_TYPES:
                logger.info("Skipping %s (%s)", hit["title"], kind)
                continue
            chosen = hit
            break

        if not chosen or not summary:
            raise WikiFetchError(
                f"Could not find a standard Wikipedia article for {topic!r}"
            )

        title = str(summary.get("title") or chosen["title"])
        extract = _extracts_wiki(client, title)
        if not extract:
            logger.warning("TextExtracts empty for %s — HTML fallback", title)
            extract = _extracts_html_fallback(client, title)
        if not extract:
            extract = str(summary.get("extract") or "")
        if not extract:
            raise WikiFetchError(f"No article text for {title!r}")

        lead, sections = parse_sections(extract)
        if not lead:
            lead = str(summary.get("extract") or "").strip()
        source = _cap_source(lead, sections, max_chars)
        kept = [s for s in sections if _keep_section(s["title"])]
        thumbnail = ""
        thumb = summary.get("originalimage") or summary.get("thumbnail") or {}
        if isinstance(thumb, dict):
            thumbnail = str(thumb.get("source") or thumb.get("url") or "")

        content_urls = summary.get("content_urls") or {}
        desktop = content_urls.get("desktop") if isinstance(content_urls, dict) else {}
        url = ""
        if isinstance(desktop, dict):
            url = str(desktop.get("page") or "")
        if not url:
            url = f"https://en.wikipedia.org/wiki/{quote(_title_key(title))}"

        article = {
            "topic": topic,
            "title": title,
            "key": str(summary.get("titles", {}).get("canonical") or _title_key(title))
            if isinstance(summary.get("titles"), dict)
            else _title_key(title),
            "url": url,
            "description": str(summary.get("description") or chosen.get("description") or ""),
            "extract_type": str(summary.get("type") or "standard"),
            "thumbnail": thumbnail,
            "lead": lead,
            "sections": kept,
            "source_text": source,
            "mock": False,
        }
        logger.info("Resolved %r → %s (%s chars)", topic, title, len(source))
        return article


def resolve_article(
    topic: str,
    fmt: str,
    output_dir: Path,
    *,
    mock: bool = False,
) -> dict[str, Any]:
    article = _mock_article(topic, fmt) if mock else fetch_article(topic, fmt)
    path = output_dir / "article.json"
    path.write_text(json.dumps(article, indent=2), encoding="utf-8")
    return article
