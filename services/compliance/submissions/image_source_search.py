"""Find a cited source on the web: Gemini Google Search grounding first, then search APIs (Serper or Google CSE) when
configured, then the stealth browser over Google News RSS and Bing. Candidates come back with the most authoritative first."""

import logging
import os
import re
from typing import List, Optional
from urllib.parse import quote_plus

import httpx
from google.genai import types

from services.ai.core.gemini_service import get_gemini_fallback_models
from services.ai.observability import ai_observability
from services.compliance.evaluation import source_settings as cfg
from services.compliance.media import image_text_service

logger = logging.getLogger(__name__)

_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)

def _extract_grounding_urls(response: object) -> List[str]:
    """Collect candidate web URLs from Gemini Google Search grounding metadata or response text."""
    urls: List[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        meta = getattr(candidate, "grounding_metadata", None)
        for chunk in getattr(meta, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            uri = (getattr(web, "uri", None) or "").strip()
            if uri and uri not in urls:
                urls.append(uri)
    text = (getattr(response, "text", None) or "").strip()
    for match in _URL_IN_TEXT.findall(text):
        cleaned = match.rstrip(".,;:!?)]}'\"")
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
    return urls


async def _google_news_rss_candidates(query: str) -> List[str]:
    """Query Google News RSS (no AI quota) for matching article links."""
    # Strip trailing punctuation and shorten overly long citation strings for better search recall.
    clean_q = re.sub(r"\s+", " ", query).strip().rstrip(".")
    lang, region = cfg.SEARCH_LANGUAGE, cfg.SEARCH_REGION
    url = (f"https://news.google.com/rss/search?q={quote_plus(clean_q)}&hl={lang}&gl={region}"
           f"&ceid={region}:{lang.split('-')[0]}")
    try:
        async with httpx.AsyncClient(timeout=cfg.SEARCH_HTTP_TIMEOUT_SEC, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": cfg.USER_AGENT})
            if resp.status_code != 200:
                return []
        blocks = re.findall(r"<item>(.*?)</item>", resp.text, flags=re.DOTALL)
        links: List[str] = []
        for b in blocks[:cfg.SEARCH_RESULTS]:
            m = re.search(r"<link>(.*?)</link>", b, flags=re.DOTALL)
            if m:
                u = m.group(1).strip()
                if u.startswith(("http://", "https://")):
                    links.append(u)
        return links
    except Exception as e:
        logger.debug("Google News RSS lookup failed: %s", e)
        return []


async def _playwright_search_urls(query: str, max_urls: int = cfg.SEARCH_RESULTS) -> List[str]:
    """Use the repo's Playwright Chromium browser to resolve Google News RSS links and Bing search results."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return []
    from services.shared.core.browser_stealth import new_stealth_context

    rss_links = await _google_news_rss_candidates(query)
    clean_q = re.sub(r"\s+", " ", query).strip().rstrip(".")
    found: List[str] = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await new_stealth_context(browser)
                page = await context.new_page()

                # 1. Resolve all Google News RSS redirects to real publisher URLs.
                for rss_url in rss_links[:max_urls]:
                    try:
                        await page.goto(rss_url, wait_until="domcontentloaded", timeout=cfg.SEARCH_PAGE_TIMEOUT_MS)
                        await page.wait_for_timeout(1200)
                        final = page.url
                        if final and "news.google.com" not in final and final.startswith(("http://", "https://")):
                            if final not in found:
                                found.append(final)
                    except Exception:
                        continue

                # 2. Also collect Bing search results if we still need more candidates.
                if len(found) < max_urls:
                    bing_url = f"https://www.bing.com/search?q={quote_plus(clean_q)}"
                    await page.goto(bing_url, wait_until="domcontentloaded", timeout=cfg.SEARCH_PAGE_TIMEOUT_MS)
                    hrefs = await page.eval_on_selector_all(
                        "li.b_algo h2 a",
                        "els => els.map(e => e.href).filter(Boolean)",
                    )
                    for href in hrefs:
                        if href.startswith(("http://", "https://")) and "bing.com" not in href and href not in found:
                            found.append(href)
                            if len(found) >= max_urls:
                                break
            finally:
                await browser.close()
    except Exception as e:
        logger.debug("Playwright source search failed: %s", e)
    return found


async def _search_api_urls(query: str) -> List[str]:
    """Optional sanctioned search APIs (Serper or Google Custom Search) when configured in env."""
    serper_key = (os.getenv("SERPER_API_KEY") or "").strip()
    if serper_key:
        try:
            async with httpx.AsyncClient(timeout=cfg.SEARCH_HTTP_TIMEOUT_SEC) as client:
                r = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                    json={"q": query, "num": cfg.SEARCH_RESULTS},
                )
                if r.status_code == 200:
                    return [item["link"] for item in (r.json().get("organic") or []) if item.get("link")]
        except Exception as e:
            logger.debug("Serper search failed: %s", e)
    cse_key = (os.getenv("GOOGLE_CSE_API_KEY") or "").strip()
    cse_cx = (os.getenv("GOOGLE_CSE_CX") or "").strip()
    if cse_key and cse_cx:
        try:
            async with httpx.AsyncClient(timeout=cfg.SEARCH_HTTP_TIMEOUT_SEC) as client:
                r = await client.get(
                    "https://www.googleapis.com/customsearch/v1",
                    # Custom Search returns 10 results at most per request.
                    params={"key": cse_key, "cx": cse_cx, "q": query, "num": min(cfg.SEARCH_RESULTS, 10)},
                )
                if r.status_code == 200:
                    return [item["link"] for item in (r.json().get("items") or []) if item.get("link")]
        except Exception as e:
            logger.debug("Google CSE search failed: %s", e)
    return []


async def find_source_urls_on_web(reference: str) -> List[str]:
    """Return candidate URLs via Gemini Google Search grounding, Search APIs, or Playwright + Google News RSS/Bing."""
    query = (reference or "").strip()
    if not query:
        return []
    client = image_text_service._get_client()
    prompt = (
        f"Find the exact primary web page or official document URL for this cited source:\n{query}\n\n"
        "Return the direct URL to the official memo, press release, report, or article."
    )
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0,
        http_options=types.HttpOptions(timeout=int(os.getenv("SOURCE_SEARCH_TIMEOUT_SEC", "30")) * 1000),
    )
    gemini_models = [m for m in get_gemini_fallback_models() if m.startswith("gemini-")]
    for model in gemini_models:
        try:
            resp = await ai_observability.generate(
                client, model=model, contents=prompt, config=config, feature="IMAGE_SOURCE_SEARCH",
            )
            urls = _extract_grounding_urls(resp)
            if urls:
                return urls
        except Exception as e:
            logger.warning("Web search for named source failed (%s): %s", model, e)
            continue
    api_urls = await _search_api_urls(query)
    if api_urls:
        return api_urls
    return await _playwright_search_urls(query)


async def find_source_url_on_web(reference: str) -> Optional[str]:
    """Return the top candidate URL for a named source."""
    urls = await find_source_urls_on_web(reference)
    return urls[0] if urls else None
