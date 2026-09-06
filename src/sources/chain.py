"""Multi-provider source fallback chain for topic information (Wikipedia, Simple Wiki, DuckDuckGo, LLM)."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from src.config import format_profile

logger = logging.getLogger(__name__)

USER_AGENT = (
    "WikiVideoPipeline/1.0 "
    "(https://en.wikipedia.org/wiki/Wikipedia:Reusing_Wikipedia_content; "
    "local educational tool; operator Bastian) python-httpx"
)

DEFAULT_SOURCE_CHAIN = [
    "wikipedia",
    "simple_wikipedia",
    "duckduckgo",
    "llm",
    "mock",
]

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_HEADING_RE = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$")

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


def _http_client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Api-User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
        follow_redirects=True,
        timeout=25.0,
    )


def _title_key(title: str) -> str:
    return (title or "").strip().replace(" ", "_")


def _cap_source(lead: str, sections: list[dict[str, str]], max_chars: int) -> str:
    parts: list[str] = []
    if lead:
        parts.append(lead)
    used = len(lead)
    kept: list[dict[str, str]] = []
    for section in sections:
        title = section.get("title", "").strip()
        if title.lower() in SKIP_SECTIONS:
            continue
        text = section.get("text", "").strip()
        if not text:
            continue
        chunk = f"\n\n== {title} ==\n{text}"
        if used + len(chunk) > max_chars and kept:
            break
        if used + len(chunk) > max_chars:
            remain = max(0, max_chars - used - len(title) - 10)
            trimmed = text[:remain].rsplit(" ", 1)[0]
            if trimmed:
                kept.append({"title": title, "text": trimmed})
            break
        kept.append(section)
        used += len(chunk)
        parts.append(f"== {title} ==\n{text}")
    return "\n\n".join(parts).strip()


def _parse_wiki_sections(extract: str) -> tuple[str, list[dict[str, str]]]:
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
    return "\n".join(lead_lines).strip(), sections


# ---------------------------------------------------------------------------
# Provider: Wikipedia (English)
# ---------------------------------------------------------------------------
def _fetch_wikipedia(topic: str, max_chars: int) -> dict[str, Any]:
    with _http_client() as client:
        search_resp = client.get(
            "https://en.wikipedia.org/w/rest.php/v1/search/page",
            params={"q": topic, "limit": "5"},
        )
        hits: list[dict[str, Any]] = []
        if search_resp.status_code == 200:
            hits = search_resp.json().get("pages") or []

        if not hits:
            # Fallback to Action API search
            action_resp = client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": topic,
                    "srlimit": "5",
                    "srnamespace": "0",
                    "format": "json",
                    "formatversion": "2",
                },
            )
            action_resp.raise_for_status()
            hits = (action_resp.json().get("query") or {}).get("search") or []

        if not hits:
            raise RuntimeError(f"No English Wikipedia articles found for {topic!r}")

        summary: dict[str, Any] | None = None
        chosen_title = ""
        for hit in hits:
            title = str(hit.get("title") or hit.get("key") or "").strip()
            if not title:
                continue
            sum_resp = client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(_title_key(title), safe='')}"
            )
            if sum_resp.status_code != 200:
                continue
            data = sum_resp.json()
            if str(data.get("type", "")).lower() in ("disambiguation", "mainpage", "no-extract"):
                continue
            summary = data
            chosen_title = title
            break

        if not summary or not chosen_title:
            raise RuntimeError(f"No suitable English Wikipedia summary found for {topic!r}")

        # Fetch full text extracts
        ext_resp = client.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "prop": "extracts",
                "explaintext": "1",
                "exsectionformat": "wiki",
                "titles": chosen_title,
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
            },
        )
        extract = ""
        if ext_resp.status_code == 200:
            pages = (ext_resp.json().get("query") or {}).get("pages") or []
            if pages:
                extract = str(pages[0].get("extract") or "").strip()

        if not extract:
            extract = str(summary.get("extract") or "")

        lead, sections = _parse_wiki_sections(extract)
        if not lead:
            lead = str(summary.get("extract") or "").strip()

        thumbnail = ""
        thumb = summary.get("originalimage") or summary.get("thumbnail") or {}
        if isinstance(thumb, dict):
            thumbnail = str(thumb.get("source") or thumb.get("url") or "")

        source_text = _cap_source(lead, sections, max_chars)
        url = (
            (summary.get("content_urls", {}).get("desktop", {}) or {}).get("page")
            or f"https://en.wikipedia.org/wiki/{quote(_title_key(chosen_title))}"
        )

        return {
            "topic": topic,
            "title": str(summary.get("title") or chosen_title),
            "key": _title_key(chosen_title),
            "url": url,
            "description": str(summary.get("description") or ""),
            "extract_type": "wikipedia",
            "thumbnail": thumbnail,
            "lead": lead,
            "sections": sections,
            "source_text": source_text,
            "source_provider": "wikipedia",
            "mock": False,
        }


# ---------------------------------------------------------------------------
# Provider: Simple English Wikipedia
# ---------------------------------------------------------------------------
def _fetch_simple_wikipedia(topic: str, max_chars: int) -> dict[str, Any]:
    with _http_client() as client:
        # Action search on simple.wikipedia.org
        search_resp = client.get(
            "https://simple.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": topic,
                "srlimit": "3",
                "format": "json",
                "formatversion": "2",
            },
        )
        search_resp.raise_for_status()
        hits = (search_resp.json().get("query") or {}).get("search") or []
        if not hits:
            raise RuntimeError(f"No Simple Wikipedia articles found for {topic!r}")

        summary: dict[str, Any] | None = None
        chosen_title = ""
        for hit in hits:
            title = str(hit.get("title") or "").strip()
            if not title:
                continue
            sum_resp = client.get(
                f"https://simple.wikipedia.org/api/rest_v1/page/summary/{quote(_title_key(title), safe='')}"
            )
            if sum_resp.status_code != 200:
                continue
            data = sum_resp.json()
            if str(data.get("type", "")).lower() in ("disambiguation", "mainpage", "no-extract"):
                continue
            summary = data
            chosen_title = title
            break

        if not summary or not chosen_title:
            raise RuntimeError(f"No suitable Simple Wikipedia summary for {topic!r}")

        ext_resp = client.get(
            "https://simple.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "prop": "extracts",
                "explaintext": "1",
                "exsectionformat": "wiki",
                "titles": chosen_title,
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
            },
        )
        extract = ""
        if ext_resp.status_code == 200:
            pages = (ext_resp.json().get("query") or {}).get("pages") or []
            if pages:
                extract = str(pages[0].get("extract") or "").strip()

        if not extract:
            extract = str(summary.get("extract") or "")

        lead, sections = _parse_wiki_sections(extract)
        if not lead:
            lead = str(summary.get("extract") or "").strip()

        thumbnail = ""
        thumb = summary.get("originalimage") or summary.get("thumbnail") or {}
        if isinstance(thumb, dict):
            thumbnail = str(thumb.get("source") or thumb.get("url") or "")

        source_text = _cap_source(lead, sections, max_chars)
        url = (
            (summary.get("content_urls", {}).get("desktop", {}) or {}).get("page")
            or f"https://simple.wikipedia.org/wiki/{quote(_title_key(chosen_title))}"
        )

        return {
            "topic": topic,
            "title": str(summary.get("title") or chosen_title),
            "key": _title_key(chosen_title),
            "url": url,
            "description": str(summary.get("description") or ""),
            "extract_type": "simple_wikipedia",
            "thumbnail": thumbnail,
            "lead": lead,
            "sections": sections,
            "source_text": source_text,
            "source_provider": "simple_wikipedia",
            "mock": False,
        }


# ---------------------------------------------------------------------------
# Provider: DuckDuckGo Instant Answer API
# ---------------------------------------------------------------------------
def _fetch_duckduckgo(topic: str, max_chars: int) -> dict[str, Any]:
    with _http_client() as client:
        resp = client.get(
            "https://api.duckduckgo.com/",
            params={
                "q": topic,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        heading = str(data.get("Heading") or topic).strip()
        abstract = str(data.get("Abstract") or data.get("AbstractText") or "").strip()
        abstract_url = str(data.get("AbstractURL") or "")

        if not abstract:
            # Check related topics for text
            related = data.get("RelatedTopics") or []
            for item in related:
                if isinstance(item, dict) and item.get("Text"):
                    abstract = str(item["Text"]).strip()
                    break

        if not abstract:
            raise RuntimeError(f"No DuckDuckGo instant answer found for {topic!r}")

        sections: list[dict[str, str]] = []
        related = data.get("RelatedTopics") or []
        for i, item in enumerate(related[:4]):
            if isinstance(item, dict) and item.get("Text"):
                text = str(item["Text"]).strip()
                sec_title = f"Key Insight {i + 1}"
                if " - " in text:
                    parts = text.split(" - ", 1)
                    sec_title = parts[0][:40].strip()
                    text = parts[1].strip()
                sections.append({"title": sec_title, "text": text})

        thumbnail = str(data.get("Image") or "")
        if thumbnail and not thumbnail.startswith("http"):
            thumbnail = f"https://duckduckgo.com{thumbnail}"

        source_text = _cap_source(abstract, sections, max_chars)

        return {
            "topic": topic,
            "title": heading or topic,
            "key": _title_key(heading or topic),
            "url": abstract_url or f"https://duckduckgo.com/?q={quote(topic)}",
            "description": f"Knowledge entry for {heading}",
            "extract_type": "duckduckgo",
            "thumbnail": thumbnail,
            "lead": abstract,
            "sections": sections,
            "source_text": source_text,
            "source_provider": "duckduckgo",
            "mock": False,
        }


# ---------------------------------------------------------------------------
# Provider: LLM Knowledge Synthesis
# ---------------------------------------------------------------------------
def _fetch_llm(topic: str, max_chars: int) -> dict[str, Any]:
    from src.llm.chain import get_llm_chain

    chain = get_llm_chain()
    system_prompt = (
        "You are an encyclopedic research assistant. "
        "Produce an accurate, educational, and structured factual summary of the topic. "
        "Respond with a JSON object with this shape:\n"
        '{"title": "Title", "description": "Short summary phrase", "lead": "1-2 engaging overview paragraphs", '
        '"sections": [{"title": "Heading 1", "text": "Factual details"}, {"title": "Heading 2", "text": "Factual details"}]}'
    )
    user_prompt = f"Topic: {topic}\nProvide an informative, factual summary suitable for an educational video."

    data = chain.complete_json(system_prompt, user_prompt, temperature=0.3, max_tokens=2000)
    if not data or not data.get("lead"):
        raise RuntimeError(f"LLM chain failed to produce factual article for {topic!r}")

    title = str(data.get("title") or topic).strip()
    lead = str(data.get("lead") or "").strip()
    description = str(data.get("description") or f"Educational guide to {title}").strip()
    raw_sections = data.get("sections") or []
    sections: list[dict[str, str]] = []
    if isinstance(raw_sections, list):
        for s in raw_sections:
            if isinstance(s, dict) and s.get("title") and s.get("text"):
                sections.append({"title": str(s["title"]).strip(), "text": str(s["text"]).strip()})

    source_text = _cap_source(lead, sections, max_chars)

    return {
        "topic": topic,
        "title": title,
        "key": _title_key(title),
        "url": f"https://en.wikipedia.org/wiki/{quote(_title_key(title))}",
        "description": description,
        "extract_type": "llm_synthesis",
        "thumbnail": "",
        "lead": lead,
        "sections": sections,
        "source_text": source_text,
        "source_provider": "llm",
        "mock": False,
    }


# ---------------------------------------------------------------------------
# Provider: Local Mock / Template Fallback
# ---------------------------------------------------------------------------
def _fetch_mock(topic: str, max_chars: int) -> dict[str, Any]:
    title = topic.strip() or "General Topic"
    lead = (
        f"{title} is a subject remembered for its fascinating historical context and significance. "
        "It connects everyday curiosity with a deeper appreciation of the world."
    )
    sections = [
        {
            "title": "Origins & Significance",
            "text": (
                f"{title} first became prominent through unexpected discoveries and collective human curiosity. "
                "Researchers and historians continue to find new insights into its influence."
            ),
        },
        {
            "title": "Why It Matters Today",
            "text": (
                "The essential value lies in the turning points: the moments when ordinary concepts "
                "transformed understanding and opened doorways to modern thinking."
            ),
        },
    ]
    source_text = _cap_source(lead, sections, max_chars)
    return {
        "topic": topic,
        "title": title,
        "key": _title_key(title),
        "url": f"https://en.wikipedia.org/wiki/{quote(_title_key(title))}",
        "description": "Deterministic local fallback dossier",
        "extract_type": "mock",
        "thumbnail": "",
        "lead": lead,
        "sections": sections,
        "source_text": source_text,
        "source_provider": "mock",
        "mock": True,
    }


# ---------------------------------------------------------------------------
# SourceChain Engine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceEndpoint:
    name: str
    fetcher: Callable[[str, int], dict[str, Any]]


_PROVIDERS: dict[str, Callable[[str, int], dict[str, Any]]] = {
    "wikipedia": _fetch_wikipedia,
    "simple_wikipedia": _fetch_simple_wikipedia,
    "duckduckgo": _fetch_duckduckgo,
    "llm": _fetch_llm,
    "mock": _fetch_mock,
}


class SourceChain:
    """Ordered fallback chain for resolving topics to encyclopedic articles."""

    def __init__(self, endpoints: list[SourceEndpoint] | None = None) -> None:
        if endpoints is None:
            endpoints = [
                SourceEndpoint(name=p, fetcher=_PROVIDERS[p])
                for p in DEFAULT_SOURCE_CHAIN
                if p in _PROVIDERS
            ]
        self.endpoints = endpoints
        self.last_provider: str | None = None
        self.last_error: str | None = None

    def resolve(
        self,
        topic: str,
        fmt: str = "short",
        *,
        max_chars: int | None = None,
        mock: bool = False,
    ) -> dict[str, Any]:
        """Attempt each source provider in sequence until one succeeds."""
        topic = (topic or "").strip()
        if not topic:
            raise ValueError("topic is required")

        if max_chars is None:
            profile = format_profile(fmt)
            max_chars = int(profile.get("source_chars") or 8000)

        if mock:
            self.last_provider = "mock"
            return _fetch_mock(topic, max_chars)

        errors: list[str] = []
        for endpoint in self.endpoints:
            if endpoint.name == "mock":
                # Only use mock if all cloud/web providers failed
                continue
            try:
                logger.info("Resolving topic %r via source provider %s...", topic, endpoint.name)
                article = endpoint.fetcher(topic, max_chars)
                if article and article.get("lead"):
                    self.last_provider = endpoint.name
                    logger.info(
                        "Resolved %r via %s: %r (%d chars)",
                        topic,
                        endpoint.name,
                        article.get("title"),
                        len(article.get("source_text") or ""),
                    )
                    return article
            except Exception as exc:  # noqa: BLE001
                err_msg = str(exc)
                logger.warning(
                    "Source provider %s failed for %r: %s — trying next provider",
                    endpoint.name,
                    topic,
                    err_msg[:180],
                )
                errors.append(f"{endpoint.name}: {err_msg[:160]}")

        self.last_error = " | ".join(errors)
        logger.warning("All source providers failed for %r (%s) — using mock fallback", topic, self.last_error[:200])
        self.last_provider = "mock"
        return _fetch_mock(topic, max_chars)


_default_source_chain: SourceChain | None = None


def get_source_chain() -> SourceChain:
    global _default_source_chain
    if _default_source_chain is None:
        _default_source_chain = SourceChain()
    return _default_source_chain
