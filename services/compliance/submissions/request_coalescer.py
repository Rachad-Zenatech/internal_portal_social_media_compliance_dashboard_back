"""Short-lived request sharing for expensive submission-list reads."""

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Tuple, TypeVar

T = TypeVar("T")


class ShortLivedRequestCache:
    """Share concurrent reads and retain the completed result briefly.

    This is deliberately always enabled: it prevents a browser remount from starting
    the same database-heavy read repeatedly, including during local development.
    """

    def __init__(self, ttl_seconds: float = 2.0):
        self._ttl_seconds = ttl_seconds
        self._values: Dict[str, Tuple[float, Any]] = {}
        self._tasks: Dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    async def get_or_load(self, key: str, loader: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            now = time.monotonic()
            cached = self._values.get(key)
            if cached and cached[0] > now:
                return cached[1]
            if cached:
                self._values.pop(key, None)
            task = self._tasks.get(key)
            if task is None:
                task = asyncio.create_task(loader())
                self._tasks[key] = task

        try:
            value = await asyncio.shield(task)
        except Exception:
            raise
        finally:
            if task.done():
                async with self._lock:
                    self._tasks.pop(key, None)
                    if not task.cancelled() and task.exception() is None:
                        self._values[key] = (time.monotonic() + self._ttl_seconds, task.result())
        return value


review_queue_request_cache = ShortLivedRequestCache()
