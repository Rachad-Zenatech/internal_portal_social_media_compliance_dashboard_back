"""Is what an item links to about the topic it was collected for?

Every outside link of a collected item (see scrape_x_media.source_links) is opened and read, and flagged in
item["source_link_checks"]; the links themselves are always kept:

    {"url", "title", "relevant": True | False | None, "method": "keyword" | "ai" | "unchecked", "reason"}

Keywords decide first (the topic's own scorer on the page's title and text): a clear match is relevant, no match at all is
not. Only the in-between cases are asked of Gemini, within a budget. A link that couldn't be read, or wasn't looked at
because the budget ran out, is `relevant: None` ("unchecked") with the reason.

Limits (env): SCRAPE_LINK_CHECK=off, SCRAPE_LINK_CHECK_MAX (links per run), SCRAPE_LINK_AI_MAX (AI calls per run),
SCRAPE_LINK_MATCH_SCORE (score that counts as a clear match).
"""

import asyncio
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from google.genai import types

from scripts.scrape_x_media import add_image_text, media_fields  # noqa: F401 (media_fields is re-exported)

logger = logging.getLogger(__name__)

LINK_CHECK_ON = os.getenv("SCRAPE_LINK_CHECK", "on").strip().lower() not in ("off", "0", "false", "no")
LINK_CHECK_MAX = int(os.getenv("SCRAPE_LINK_CHECK_MAX", "30"))
LINK_AI_MAX = int(os.getenv("SCRAPE_LINK_AI_MAX", "15"))
CLEAR_MATCH_SCORE = float(os.getenv("SCRAPE_LINK_MATCH_SCORE", "4"))
PAGE_CHARS = 3000
CONCURRENCY = 4
_OWN_HOSTS = ("https://x.com/", "https://twitter.com/", "https://t.co/")

_SCHEMA = {"type": "OBJECT", "properties": {"relevant": {"type": "BOOLEAN"}, "reason": {"type": "STRING"}},
           "required": ["relevant", "reason"]}


def _entry(url: str, relevant: Optional[bool], method: str, reason: str, title: Optional[str] = None) -> Dict[str, Any]:
    return {"url": url, "title": title, "relevant": relevant, "method": method, "reason": reason}


async def _ask_ai(topic: Dict[str, Any], item_text: str, title: str, page: str) -> Optional[Dict[str, Any]]:
    """Gemini's call on a link the keywords couldn't settle, or None when no model could answer."""
    from services.ai.observability import ai_observability
    from services.compliance.media import image_text_service as reader

    prompt = (
        "We collected a social post or headline for the topic below, and it links to a web page. Is the page about this "
        "topic (the same subject, not merely sharing a word)? Answer relevant true or false with a one-sentence reason.\n\n"
        f"Topic: {topic.get('name')}\nSearch: {topic.get('live_query') or ', '.join(topic.get('keywords') or [])}\n"
        f"Post: {item_text[:400]}\nPage title: {title}\nPage text: {page[:PAGE_CHARS]}")
    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=_SCHEMA, temperature=0,
                                         http_options=types.HttpOptions(timeout=reader.TIMEOUT_SEC * 1000))
    try:
        client = reader._get_client()
    except RuntimeError:
        return None
    for model in reader.MODELS:
        try:
            resp = await ai_observability.generate(client, model=model, contents=prompt, config=config,
                                                   feature="LINK_RELEVANCE")
            return json.loads(resp.text or "{}")
        except Exception as e:
            logger.info("Link relevance: %s unavailable (%s)", model, str(e)[:100])
    return None


async def _check_link(url: str, item: Dict[str, Any], topic: Dict[str, Any], score: Callable[[str, Dict], float],
                      ai_left: List[int]) -> Dict[str, Any]:
    from services.compliance.evaluation import source_fetch_service as fetcher

    page = await fetcher.fetch(url)
    if not page.ok or not (page.text or "").strip():
        return _entry(url, None, "unchecked", page.error or "The page couldn't be read.", page.title)
    title = page.title or ""
    s = score(f"{title}\n{page.text[:PAGE_CHARS]}", topic)
    if s >= CLEAR_MATCH_SCORE:
        return _entry(url, True, "keyword", f"The page matches the topic's keywords (score {s:g}).", title)
    if s <= 0:
        return _entry(url, False, "keyword", "The page mentions none of the topic's keywords.", title)
    if ai_left[0] <= 0:
        return _entry(url, None, "unchecked", f"A weak keyword match (score {s:g}) and no AI checks left this run.", title)
    ai_left[0] -= 1
    verdict = await _ask_ai(topic, item.get("text", ""), title, page.text)
    if verdict is None:
        return _entry(url, None, "unchecked", f"A weak keyword match (score {s:g}); no AI model could judge it.", title)
    return _entry(url, bool(verdict.get("relevant")), "ai", str(verdict.get("reason") or "")[:300], title)


async def flag_link_relevance(items: List[Dict[str, Any]], topic: Dict[str, Any], score: Callable[[str, Dict], float],
                              budget: List[int], ai_left: List[int]) -> None:
    """Set item["source_link_checks"] for the outside links of these items (all collected for `topic`)."""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(item: Dict[str, Any], url: str) -> Dict[str, Any]:
        async with sem:
            try:
                return await _check_link(url, item, topic, score, ai_left)
            except Exception as e:
                return _entry(url, None, "unchecked", f"The check failed: {str(e)[:120]}")

    for item in items:
        outside = [u for u in item.get("source_links") or [] if not u.startswith(_OWN_HOSTS)]
        if not outside:
            continue
        outside = outside[:max(budget[0], 0)]
        budget[0] -= len(outside)
        item["source_link_checks"] = list(await asyncio.gather(*(one(item, u) for u in outside)))


async def enrich_items(topics: List[Dict[str, Any]], homes: Dict[str, List[Dict[str, Any]]], per_topic: int,
                       score: Callable[[str, Dict], float]) -> None:
    """Read the text in the images and flag the links of everything that will be kept (`per_topic` items each)."""
    kept = {t["external_id"]: homes[t["external_id"]][:per_topic] for t in topics}
    await add_image_text([c for items in kept.values() for c in items])
    if not LINK_CHECK_ON:
        return
    budget, ai_left = [LINK_CHECK_MAX], [LINK_AI_MAX]
    for t in topics:
        await flag_link_relevance(kept[t["external_id"]], t, score, budget, ai_left)
