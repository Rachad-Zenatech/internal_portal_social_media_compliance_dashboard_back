"""Small cached health snapshot used by the integrations status widget."""

import asyncio
import time
from typing import Any

from postgresql_db.database import fetch_one

_cache: tuple[float, dict[str, Any]] | None = None
_CACHE_SECONDS = 15


async def get_integration_status() -> dict[str, Any]:
    """Avoid a database readiness query for every component mount."""
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    try:
        await asyncio.wait_for(fetch_one("SELECT 1 AS ready"), timeout=2.0)
        db_ok = True
    except Exception:
        db_ok = False
    result = {
        "status": "healthy" if db_ok else "degraded",
        "services": [
            {"id": "database_pool", "name": "PostgreSQL Connection Pool", "state": "CONNECTED" if db_ok else "DISCONNECTED", "status": "online" if db_ok else "offline", "description": "Asyncpg connection pool with Tier 1 in-memory caching", "mode": "Active Pool", "failure_count": 0 if db_ok else 1},
            {"id": "realtime_sse", "name": "Real-time Notification Stream (SSE)", "state": "STREAMING", "status": "online", "description": "Server-Sent Events with 30s zero-DB keep-alive heartbeat", "mode": "Live Stream", "failure_count": 0},
        ],
    }
    _cache = (now, result)
    return result
