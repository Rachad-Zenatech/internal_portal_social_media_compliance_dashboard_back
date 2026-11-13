"""Second try at reading a source page: a headless browser with stealth, for pages the plain fetch can't read because
they are built by JavaScript or sit behind bot protection that turns away plain requests.

The link is still treated as hostile: every request the page makes (the page itself, redirects, scripts, data) has
its host checked the same way source_fetch_service checks it, and anything pointing at a private address is refused.
Images, fonts and media aren't loaded. Nothing here uses AI.
"""

import asyncio
import hashlib
import logging
import os
from typing import Dict, Optional
from urllib.parse import urlsplit

from services.compliance.evaluation import source_fetch_service as sf

logger = logging.getLogger(__name__)

ENABLED = os.getenv("SOURCE_FETCH_BROWSER", "true").strip().lower() not in {"0", "false", "no", "off"}
TIMEOUT_MS = int(float(os.getenv("SOURCE_FETCH_BROWSER_TIMEOUT_SEC", "25")) * 1000)
# Time for a script-built page to put its text in place after it has loaded.
SETTLE_MS = int(os.getenv("SOURCE_FETCH_BROWSER_SETTLE_MS", "2000"))
# Why the plain fetch failed, for which a browser may do better.
RETRY_STATUSES = {"FORBIDDEN", "EMPTY", "ERROR", "TIMEOUT"}
_SKIPPED_RESOURCES = {"image", "media", "font"}


async def _host_allowed(url: str, verdicts: Dict[str, bool]) -> bool:
    """Whether a request may go out: http(s) only, and the host must not resolve to a private address."""
    parts = urlsplit(url)
    if parts.scheme in ("data", "blob", "about"):
        return True
    key = f"{parts.scheme}://{parts.hostname}:{parts.port}"
    if key not in verdicts:
        try:
            await sf._check_destination(url)
            verdicts[key] = True
        except sf._Refused:
            verdicts[key] = False
    return verdicts[key]


async def fetch(url: str) -> Optional[sf.FetchedSource]:
    """The page's readable text through the browser, or None when the browser can't do better."""
    if not ENABLED:
        return None
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    from services.shared.core.browser_stealth import new_stealth_context

    verdicts: Dict[str, bool] = {}
    if not await _host_allowed(url, verdicts):
        return None

    async def guard(route) -> None:
        request = route.request
        if request.resource_type in _SKIPPED_RESOURCES or not await _host_allowed(request.url, verdicts):
            await route.abort()
        else:
            await route.continue_()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await new_stealth_context(browser, java_script_enabled=True)
                await context.route("**/*", guard)
                page = await context.new_page()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                if response is None or response.status >= 400:
                    return None
                if "html" not in (response.headers.get("content-type") or "html"):
                    return None  # PDFs and other files are the plain fetch's job
                await page.wait_for_timeout(SETTLE_MS)
                html = await page.content()
                final = page.url
            finally:
                await browser.close()
    except Exception as e:
        logger.info("Browser fetch failed for %s: %s", url, str(e)[:160])
        return None

    if not await _host_allowed(final, verdicts):
        return None
    text, title, published = await asyncio.to_thread(sf.html_to_text, html)
    if len(text) < sf.MIN_TEXT_CHARS:
        return None
    text = text[:sf.MAX_TEXT_CHARS]
    return sf.FetchedSource(
        url=url, ok=True, status=sf.OK, final_url=final, title=title, published=published, text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
