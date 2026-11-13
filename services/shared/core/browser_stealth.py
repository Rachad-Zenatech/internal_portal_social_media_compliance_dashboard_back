"""Playwright stealth: hides the usual headless-browser giveaways (navigator.webdriver, missing plugins, the
"HeadlessChrome" user agent, ...) on a browser context, so sites are less likely to block the scrapers."""
import logging

logger = logging.getLogger(__name__)

DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

try:
    from playwright_stealth import Stealth

    _STEALTH = Stealth()
except ImportError:  # the scrape still runs, just without stealth
    _STEALTH = None


async def apply_stealth(context) -> bool:
    """Apply stealth to a Playwright browser context (every page opened from it after this).
    Returns False when playwright-stealth is not installed or could not be applied."""
    if _STEALTH is None:
        logger.warning("playwright-stealth is not installed; browsing without stealth")
        return False
    try:
        await _STEALTH.apply_stealth_async(context)
        return True
    except Exception as exc:  # never let stealth stop a scrape
        logger.warning("Could not apply playwright stealth: %s", exc)
        return False


async def new_stealth_context(browser, **kwargs):
    """A new browser context with a desktop Chrome user agent and stealth applied."""
    kwargs.setdefault("user_agent", DESKTOP_CHROME_UA)
    context = await browser.new_context(**kwargs)
    await apply_stealth(context)
    return context
