"""Read optional OAuth connections from the administration portal."""
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def fetch_admin_portal_connections() -> list[dict[str, Any]] | None:
    """Return remote connections when the optional administration portal is configured."""
    base_url = (os.getenv("ADMIN_PORTAL_API_URL") or "").strip().rstrip("/")
    if not base_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{base_url}/api/oauth/connections")
        payload = response.json() if response.status_code == 200 else None
        return payload if isinstance(payload, list) and payload else None
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("Couldn't fetch OAuth connections from the administration portal: %s", exc)
        return None
