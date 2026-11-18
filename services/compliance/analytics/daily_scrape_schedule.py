"""Daily 8:30 AM schedule for refreshing both nav public-mood scrapings (Financial ZenaTech + Today's Posts)."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        val = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(low, min(high, val))


DAILY_SCRAPE_ENABLED = os.getenv("DAILY_PUBLIC_SCRAPE_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off",
)
DAILY_SCRAPE_HOUR = _env_int("DAILY_PUBLIC_SCRAPE_HOUR", 8, low=0, high=23)
DAILY_SCRAPE_MINUTE = _env_int("DAILY_PUBLIC_SCRAPE_MINUTE", 30, low=0, high=59)
DAILY_SCRAPE_TIMEZONE = os.getenv("APP_TIMEZONE", "America/New_York")
DAILY_SCRAPE_GRACE_MINUTES = _env_int("DAILY_PUBLIC_SCRAPE_GRACE_MINUTES", 60, low=0, high=720)
MAX_SLEEP_CHUNK_SECONDS = 300.0


def _tz(tz_name: str = DAILY_SCRAPE_TIMEZONE):
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def today_scheduled_run(
    now: Optional[datetime] = None,
    *,
    hour: int = DAILY_SCRAPE_HOUR,
    minute: int = DAILY_SCRAPE_MINUTE,
    tz_name: str = DAILY_SCRAPE_TIMEZONE,
) -> datetime:
    """UTC datetime for today's scheduled run in `tz_name`."""
    tz = _tz(tz_name)
    local_now = (now or datetime.now(timezone.utc)).astimezone(tz)
    return local_now.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc)


def next_scheduled_run(
    now: Optional[datetime] = None,
    *,
    hour: int = DAILY_SCRAPE_HOUR,
    minute: int = DAILY_SCRAPE_MINUTE,
    tz_name: str = DAILY_SCRAPE_TIMEZONE,
) -> datetime:
    """Next upcoming scheduled run strictly at or after `now`, returned in UTC."""
    tz = _tz(tz_name)
    ref_utc = now or datetime.now(timezone.utc)
    if ref_utc.tzinfo is None:
        ref_utc = ref_utc.replace(tzinfo=timezone.utc)
    local_now = ref_utc.astimezone(tz)
    candidate_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate_local <= local_now:
        candidate_local = (local_now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate_local.astimezone(timezone.utc)


def schedule_description(
    *,
    enabled: bool = DAILY_SCRAPE_ENABLED,
    hour: int = DAILY_SCRAPE_HOUR,
    minute: int = DAILY_SCRAPE_MINUTE,
    tz_name: str = DAILY_SCRAPE_TIMEZONE,
) -> str:
    """Human-readable schedule summary for the nav header."""
    if not enabled:
        return "Daily auto-scrape is disabled"
    local_dt = datetime.now(_tz(tz_name)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    time_str = local_dt.strftime("%I:%M %p").lstrip("0")
    tz_abbr = local_dt.tzname() or tz_name
    return f"Daily auto-scrape at {time_str} {tz_abbr}"


def should_catch_up_missed_run(
    now: datetime,
    last_collected_at: Optional[datetime],
    *,
    hour: int = DAILY_SCRAPE_HOUR,
    minute: int = DAILY_SCRAPE_MINUTE,
    tz_name: str = DAILY_SCRAPE_TIMEZONE,
    grace_minutes: int = DAILY_SCRAPE_GRACE_MINUTES,
) -> bool:
    """True when the server started shortly after today's 8:30 AM window and no scrape has run since then."""
    if grace_minutes <= 0:
        return False
    today_run = today_scheduled_run(now, hour=hour, minute=minute, tz_name=tz_name)
    if not (today_run <= now <= today_run + timedelta(minutes=grace_minutes)):
        return False
    return last_collected_at is None or last_collected_at < today_run


async def _run_guarded(
    run_scrape: Callable[[], Awaitable[None]],
    is_running: Callable[[], bool],
    load_last_collected_at: Callable[[], Awaitable[Optional[datetime]]],
    target: datetime,
    try_lock: Optional[Callable[[], Awaitable[bool]]],
    release_lock: Optional[Callable[[], Awaitable[None]]],
) -> None:
    if is_running():
        return
    last = await load_last_collected_at()
    if last is not None and last >= target:
        return
    if try_lock is not None and not await try_lock():
        return
    try:
        logger.info("Running scheduled 8:30 AM public mood scrape")
        await run_scrape()
    finally:
        if release_lock is not None:
            try:
                await release_lock()
            except Exception:
                logger.warning("Failed to release daily scrape advisory lock", exc_info=True)


async def daily_scrape_loop(
    run_scrape: Callable[[], Awaitable[None]],
    is_running: Callable[[], bool],
    load_last_collected_at: Callable[[], Awaitable[Optional[datetime]]],
    *,
    enabled: bool = DAILY_SCRAPE_ENABLED,
    hour: int = DAILY_SCRAPE_HOUR,
    minute: int = DAILY_SCRAPE_MINUTE,
    tz_name: str = DAILY_SCRAPE_TIMEZONE,
    grace_minutes: int = DAILY_SCRAPE_GRACE_MINUTES,
    try_lock: Optional[Callable[[], Awaitable[bool]]] = None,
    release_lock: Optional[Callable[[], Awaitable[None]]] = None,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_iterations: Optional[int] = None,
) -> None:
    """Run `run_scrape` once per day at `hour:minute` in `tz_name` (default 8:30 AM America/New_York)."""
    if not enabled:
        return
    now = now_fn()
    target = (
        today_scheduled_run(now, hour=hour, minute=minute, tz_name=tz_name)
        if should_catch_up_missed_run(now, await load_last_collected_at(), hour=hour, minute=minute, tz_name=tz_name, grace_minutes=grace_minutes)
        else next_scheduled_run(now, hour=hour, minute=minute, tz_name=tz_name)
    )
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        now = now_fn()
        if now >= target:
            try:
                await _run_guarded(run_scrape, is_running, load_last_collected_at, target, try_lock, release_lock)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled public mood scrape failed")
            target = next_scheduled_run(now_fn() + timedelta(seconds=1), hour=hour, minute=minute, tz_name=tz_name)
            continue
        remaining = max(1.0, (target - now).total_seconds())
        await sleep_fn(min(remaining, MAX_SLEEP_CHUNK_SECONDS))
