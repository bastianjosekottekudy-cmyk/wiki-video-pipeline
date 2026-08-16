"""Documentary narration from a Wikipedia article via the LLM chain."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.config import format_profile, load_pipeline_config

logger = logging.getLogger(__name__)

WORDS_PER_SEC = 2.3
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

_NARRATION_SYSTEM = (
    "You write spoken English for a Wikipedia explainer video. "
    "Write like a documentary host talking to camera — complete sentences, "
    "natural pacing, a hook in the first line. "
    "Stay faithful to the source. Never invent facts, dates, quotes, or numbers. "
    "Never read citations, URLs, section titles as labels, or license text. "
    "Make it interesting: surprising true details, a story arc, why it matters. "
    "Reply with JSON only — no markdown fences."
)


def _clean_for_speech(text: str) -> str:
    cleaned = _URL_RE.sub("", text or "")
    cleaned = re.sub(r"\[[0-9]+\]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _target_words(fmt: str) -> int | None:
    profile = format_profile(fmt)
    target = float(profile.get("target_duration_sec") or 0)
    max_dur = float(profile.get("max_video_duration_sec") or 0)
    seconds = target or max_dur
    if seconds <= 0:
        return None
    return max(80, int(seconds * WORDS_PER_SEC * 0.92))


def _template_script(article: dict[str, Any], fmt: str) -> dict[str, Any]:
    title = str(article.get("title") or article.get("topic") or "This topic")
    lead = _clean_for_speech(str(article.get("lead") or article.get("description") or title))
    hook = lead.split(". ")[0].strip()
    if hook and not hook.endswith("."):
        hook += "."
    if not hook:
        hook = f"Here is the story of {title}."

    chapters: list[dict[str, str]] = []
    for section in article.get("sections") or []:
        heading = str(section.get("title") or "Chapter").strip()
        body = _clean_for_speech(str(section.get("text") or ""))
        if not body:
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
        take = sentences[:4] if fmt == "short" else sentences[:8]
        narration = " ".join(take)
        if narration:
            chapters.append({"heading": heading, "narration": narration})
        if fmt == "short" and len(chapters) >= 4:
            break
        if fmt == "video" and len(chapters) >= 10:
            break

    if not chapters:
        chapters = [
            {
                "heading": title,
                "narration": lead or f"{title} is worth knowing because the details are stranger than the headline.",
            }
        ]

    outro = "That is the story. Wikipedia is the source — go read the full article if you want more."
    return {
        "title": title,
        "hook": hook,
        "chapters": chapters,
        "outro": outro,
        "provider": "template",
    }


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    return data


def _normalize_script(data: dict[str, Any], article: dict[str, Any]) -> dict[str, Any]:
    title = _clean_for_speech(str(data.get("title") or article.get("title") or "Topic"))
    hook = _clean_for_speech(str(data.get("hook") or ""))
    outro = _clean_for_speech(str(data.get("outro") or ""))
    chapters: list[dict[str, str]] = []
    raw_chapters = data.get("chapters") or []
    if isinstance(raw_chapters, list):
        for item in raw_chapters:
            if isinstance(item, str):
                text = _clean_for_speech(item)
                if text:
                    chapters.append({"heading": title, "narration": text})
                continue
            if not isinstance(item, dict):
                continue
            heading = str(item.get("heading") or item.get("title") or title).strip()
            narration = _clean_for_speech(str(item.get("narration") or item.get("text") or ""))
            if narration:
                chapters.append({"heading": heading or title, "narration": narration})
    if not hook:
        hook = _clean_for_speech(str(article.get("lead") or title).split(". ")[0])
        if hook and not hook.endswith("."):
            hook += "."
    if not chapters:
        fallback = _template_script(article, "video")
        chapters = fallback["chapters"]
    if not outro:
        outro = "That is the story, drawn from Wikipedia."
    return {"title": title, "hook": hook, "chapters": chapters, "outro": outro}


def _word_count(script: dict[str, Any]) -> int:
    parts = [script.get("hook") or "", script.get("outro") or ""]
    parts.extend(c.get("narration") or "" for c in script.get("chapters") or [])
    return len(" ".join(parts).split())


def generate_script(
    article: dict[str, Any],
    fmt: str,
    output_dir: Path,
) -> dict[str, Any]:
    config = load_pipeline_config()
    style = str((config.get("script") or {}).get("style") or "")
    word_budget = _target_words(fmt)
    source = str(article.get("source_text") or article.get("lead") or "")
    title = str(article.get("title") or article.get("topic") or "Topic")

    length_rule = (
        f"Keep the whole spoken script under {word_budget} words."
        if word_budget
        else "Take the time the story needs. Prefer 3 to 12 minutes of speech, not a lecture."
    )
    chapter_rule = (
        "Use 3 to 5 short chapters."
        if fmt == "short"
        else "Use as many chapters as the article needs, typically 5 to 10."
    )

    user = (
        f"Topic: {title}\n"
        f"Format: {'vertical YouTube Short' if fmt == 'short' else 'landscape YouTube video'}\n"
        f"{length_rule} {chapter_rule}\n"
        f"Style: {style}\n\n"
        "Source (Wikipedia, already trimmed). Invent nothing beyond this:\n"
        f"{source}\n\n"
        "JSON shape:\n"
        '{"title":"...","hook":"spoken opener","chapters":[{"heading":"on-screen heading","narration":"spoken beat"}],"outro":"spoken close"}'
    )

    script: dict[str, Any] | None = None
    provider = "template"
    try:
        from src.llm.chain import TEMPLATE_SENTINEL, get_llm_chain

        chain = get_llm_chain()
        result = chain.complete(
            _NARRATION_SYSTEM,
            user,
            max_tokens=4000 if fmt == "video" else 1400,
        )
        if result and result != TEMPLATE_SENTINEL:
            parsed = _parse_llm_json(str(result))
            if parsed:
                script = _normalize_script(parsed, article)
                provider = "chain"
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM chain failed, using template: %s", exc)

    if script is None:
        script = _template_script(article, fmt)
        provider = "template"

    if word_budget:
        # Soft trim chapters from the end if the model ran long
        while _word_count(script) > word_budget + 40 and len(script["chapters"]) > 2:
            script["chapters"].pop()

    script["provider"] = provider
    script_path = output_dir / "script.txt"
    lines = [script["hook"], ""]
    for chapter in script["chapters"]:
        lines.append(chapter["narration"])
        lines.append("")
    lines.append(script["outro"])
    script_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    segments = {
        "hook": script["hook"],
        "chapters": [c["narration"] for c in script["chapters"]],
        "headings": [c["heading"] for c in script["chapters"]],
        "outro": script["outro"],
    }
    (output_dir / "script_segments.json").write_text(
        json.dumps(segments, indent=2), encoding="utf-8"
    )
    (output_dir / "script_meta.json").write_text(
        json.dumps(
            {
                "title": script["title"],
                "provider": provider,
                "format": fmt,
                "word_count": _word_count(script),
                "chapters": len(script["chapters"]),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "Script ready (%s, %s words, %s chapters)",
        provider,
        _word_count(script),
        len(script["chapters"]),
    )
    return script
