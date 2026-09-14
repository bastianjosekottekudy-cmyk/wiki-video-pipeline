"""Topic discovery and selection with strict deduplication against uploaded runs."""

from __future__ import annotations

import logging
import random
from typing import Any

import httpx

from src.config import load_topics_config
from src.db import store

logger = logging.getLogger(__name__)

USER_AGENT = (
    "WikiVideoPipeline/1.0 "
    "(https://en.wikipedia.org/wiki/Wikipedia:Reusing_Wikipedia_content; "
    "local educational tool; operator Bastian) python-httpx"
)


def _fetch_wikipedia_featured_candidates(limit: int = 50) -> list[str]:
    """Fetch featured/vital article titles from English Wikipedia with filtering for clean educational subjects."""
    import string
    try:
        letter = random.choice(string.ascii_uppercase)
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0) as client:
            resp = client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "categorymembers",
                    "cmtitle": "Category:Featured_articles",
                    "cmnamespace": "0",
                    "cmlimit": str(limit),
                    "cmstartsortkeyprefix": letter,
                    "format": "json",
                    "formatversion": "2",
                },
            )
            if resp.status_code == 200:
                members = (resp.json().get("query") or {}).get("categorymembers") or []
                titles = [
                    str(m.get("title") or "").strip()
                    for m in members
                    if str(m.get("title") or "").strip()
                ]
                disallowed = (
                    "(film)", "(album)", "(song)", "(season", "(TV series)",
                    "division", "brigade", "regiment", "Route ", "highway",
                    "championship", "tournament", "election", "electoral",
                )
                return [
                    t for t in titles
                    if t and t[0].isalpha()
                    and not t.startswith("List of ")
                    and ":" not in t
                    and not any(d.lower() in t.lower() for d in disallowed)
                ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch featured articles from Wikipedia: %s", exc)
    return []


def _fetch_simple_wiki_random_candidates(limit: int = 20) -> list[str]:
    """Fetch random article titles from Simple English Wikipedia."""
    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15.0) as client:
            resp = client.get(
                "https://simple.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "random",
                    "rnnamespace": "0",
                    "rnlimit": str(limit),
                    "format": "json",
                    "formatversion": "2",
                },
            )
            if resp.status_code == 200:
                random_items = (resp.json().get("query") or {}).get("random") or []
                titles = [
                    str(m.get("title") or "").strip()
                    for m in random_items
                    if str(m.get("title") or "").strip()
                ]
                disallowed = ("(film)", "(album)", "(song)", "(season", "division", "Route ")
                return [
                    t for t in titles
                    if t and t[0].isalpha()
                    and not t.startswith("List of ")
                    and ":" not in t
                    and not any(d.lower() in t.lower() for d in disallowed)
                ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to fetch random articles from Simple Wikipedia: %s", exc)
    return []


def _generate_llm_topic_candidates(categories: list[str], count: int = 8) -> list[str]:
    """Use LLM chain to suggest novel, fascinating educational topics matching configured categories."""
    try:
        from src.llm.chain import get_llm_chain

        chain = get_llm_chain()
        cats = ", ".join(categories) if categories else "Science & Astronomy, Ancient History & Archaeology, Unsolved Mysteries & Curiosities, Inventions & Pioneers"
        system_prompt = (
            "You are an educational video producer. "
            "Generate captivating, concise topic titles (1-4 words) that correspond to real Wikipedia articles. "
            "Suitable for 60-second educational documentary shorts. "
            "Respond with a JSON object: {\"topics\": [\"Topic 1\", \"Topic 2\", ...]}"
        )
        user_prompt = (
            f"Generate {count} fascinating, concise topics in the following themes: {cats}. "
            "Use standard Wikipedia article title format (e.g., \"Antikythera mechanism\", \"James Webb Space Telescope\", \"Tunguska event\")."
        )
        data = chain.complete_json(system_prompt, user_prompt, temperature=0.7, max_tokens=600)
        topics = data.get("topics") or []
        if isinstance(topics, list):
            return [str(t).strip() for t in topics if str(t).strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM topic candidate generation failed: %s", exc)
    return []


def replenish_topics_pool(count: int = 10) -> list[str]:
    """
    Discover fresh un-uploaded educational topics prioritizing LLM generated topics
    matching configured categories, followed by Wikipedia Featured articles,
    and append them to the configured topics.pool in pipeline.yaml.
    """
    from src.config import load_topics_config, update_topics_pool

    cfg = load_topics_config()
    current_pool = list(cfg.get("pool") or [])
    current_keys = {store.normalize_topic_key(t) for t in current_pool}
    covered_keys = store.get_covered_topics()

    candidates: list[str] = []

    # 1. Prioritize LLM topic generation using configured channel categories
    categories = cfg.get("categories") or []
    for topic in _generate_llm_topic_candidates(categories, count=count * 2):
        norm = store.normalize_topic_key(topic)
        if norm and norm not in current_keys and norm not in covered_keys and not store.is_topic_covered(topic):
            candidates.append(topic)
            current_keys.add(norm)
            if len(candidates) >= count:
                break

    # 2. Fetch featured candidates from English Wikipedia if needed
    if len(candidates) < count:
        for topic in _fetch_wikipedia_featured_candidates(limit=60):
            norm = store.normalize_topic_key(topic)
            if norm and norm not in current_keys and norm not in covered_keys and not store.is_topic_covered(topic):
                candidates.append(topic)
                current_keys.add(norm)
                if len(candidates) >= count:
                    break

    # 3. If still needed, fetch random candidates from Simple Wikipedia
    if len(candidates) < count:
        for topic in _fetch_simple_wiki_random_candidates(limit=30):
            norm = store.normalize_topic_key(topic)
            if norm and norm not in current_keys and norm not in covered_keys and not store.is_topic_covered(topic):
                candidates.append(topic)
                current_keys.add(norm)
                if len(candidates) >= count:
                    break

    if candidates:
        new_pool = current_pool + candidates
        update_topics_pool(new_pool)
        logger.info("Replenished topic pool with %d fresh topic(s): %s", len(candidates), candidates)

    return candidates


def pick_random_topics(count: int = 1) -> list[str]:
    """
    Select non-repeated topics for shorts generation.
    Excludes any topic that was previously covered (completed video, running/queued, or uploaded).
    Priority:
      1. Configured topic pool in pipeline.yaml (auto-replenished when low)
      2. Wikipedia featured/vital articles
      3. Simple Wikipedia random articles
      4. LLM topic generation
    """
    count = max(1, int(count))
    cfg = load_topics_config()
    pool = cfg.get("pool") or []
    categories = cfg.get("categories") or []

    chosen: list[str] = []
    chosen_keys: set[str] = set()

    # Priority 1: Configured pool
    uncovered_pool = [t for t in pool if not store.is_topic_covered(t)]

    # Auto-refill pool if un-covered topics are running low (< 10 remaining)
    if len(uncovered_pool) < 10:
        logger.info("Topic pool has only %d un-covered topic(s) remaining; auto-refilling...", len(uncovered_pool))
        replenish_topics_pool(count=20)
        cfg = load_topics_config()
        pool = cfg.get("pool") or []
        uncovered_pool = [t for t in pool if not store.is_topic_covered(t)]

    if uncovered_pool:
        shuffled = list(uncovered_pool)
        random.shuffle(shuffled)
        for topic in shuffled:
            norm = store.normalize_topic_key(topic)
            if norm not in chosen_keys:
                chosen.append(topic)
                chosen_keys.add(norm)
                if len(chosen) >= count:
                    logger.info("Selected %d topic(s) from configured pool: %s", len(chosen), chosen)
                    return chosen

    logger.info("Configured pool has %d topic(s); need %d more. Discovering...", len(chosen), count - len(chosen))

    # Priority 2: Wikipedia Featured / Vital Articles
    featured = _fetch_wikipedia_featured_candidates(limit=50)
    random.shuffle(featured)
    for topic in featured:
        norm = store.normalize_topic_key(topic)
        if norm not in chosen_keys and not store.is_topic_covered(topic):
            chosen.append(topic)
            chosen_keys.add(norm)
            if len(chosen) >= count:
                logger.info("Selected topics with Wikipedia featured discovery: %s", chosen)
                return chosen

    # Priority 3: Simple Wikipedia Random Articles
    simple_candidates = _fetch_simple_wiki_random_candidates(limit=30)
    random.shuffle(simple_candidates)
    for topic in simple_candidates:
        norm = store.normalize_topic_key(topic)
        if norm not in chosen_keys and not store.is_topic_covered(topic):
            chosen.append(topic)
            chosen_keys.add(norm)
            if len(chosen) >= count:
                logger.info("Selected topics with Simple Wikipedia discovery: %s", chosen)
                return chosen

    # Priority 4: LLM Topic Generator
    llm_candidates = _generate_llm_topic_candidates(categories, count=count * 3)
    for topic in llm_candidates:
        norm = store.normalize_topic_key(topic)
        if norm not in chosen_keys and not store.is_topic_covered(topic):
            chosen.append(topic)
            chosen_keys.add(norm)
            if len(chosen) >= count:
                logger.info("Selected topics with LLM discovery: %s", chosen)
                return chosen

    if not chosen:
        fallback = f"Discovery topic {random.randint(1000, 9999)}"
        chosen.append(fallback)

    return chosen


def get_topics_status() -> dict[str, Any]:
    """Provide summary of configured pool, remaining topics, and uploaded history."""
    cfg = load_topics_config()
    pool = cfg.get("pool") or []
    categories = cfg.get("categories") or []

    uploaded_keys = store.get_uploaded_topics()
    covered_keys = store.get_covered_topics()
    uncovered_pool = [t for t in pool if store.normalize_topic_key(t) not in covered_keys]

    return {
        "pool_total": len(pool),
        "pool_remaining": len(uncovered_pool),
        "pool_remaining_topics": uncovered_pool,
        "uploaded_count": len(uploaded_keys),
        "categories": categories,
        "recent_uploaded": store.list_uploaded_topics(limit=15),
    }
