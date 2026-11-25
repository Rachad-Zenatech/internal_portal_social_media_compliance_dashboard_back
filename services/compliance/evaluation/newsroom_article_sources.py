"""Use summaries for retrieval only; verify claims against the linked newsroom article."""
import asyncio
import logging

from services.ai.observability.ai_guardrails import screen_input
from . import source_fetch_service

logger = logging.getLogger(__name__)
FETCH_CONCURRENCY = 4
FETCH_TIMEOUT_SEC = 15


async def hydrate(readings):
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(check, summary):
        try:
            async with gate:
                fetched = await asyncio.wait_for(source_fetch_service.fetch(check.url), timeout=FETCH_TIMEOUT_SEC)
            if not fetched.ok:
                raise ValueError(fetched.error or "The article could not be read.")
            if screen_input(fetched.text).verdict != "ALLOW":
                raise ValueError("The article contains wording that could steer the review and needs a person's check.")
            check.url = fetched.final_url or check.url
            check.published = fetched.published or check.published
            return check, (summary[0], fetched.text)
        except Exception:
            logger.warning("Could not read full newsroom article: %s", check.url, exc_info=True)
            check.status = "UNREADABLE"
            check.message = ("The full newsroom article could not be read. Its short summary was not used to verify "
                             "claims. Check again or attach a copy of the article.")
            # Retain the summary only for routing the affected claims. Unreadable sources never reach the model.
            return check, summary

    return list(await asyncio.gather(*(one(check, summary) for check, summary in readings)))
