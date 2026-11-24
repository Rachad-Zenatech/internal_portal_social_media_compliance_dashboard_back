"""Background & inline snapshot refresh helpers for public_mood_service."""

import asyncio
import json
import logging
from typing import Any, Dict, List, Set
from uuid import UUID

from postgresql_db.database import get_pool
from services.compliance.analytics.listening_providers import ListeningUnavailable

logger = logging.getLogger(__name__)

_refreshing_ids: Set[UUID] = set()
_bg_tasks: Set[asyncio.Task] = set()


async def refresh_stale_snapshots(
    topics: List[Dict[str, Any]],
    stale: List[Dict[str, Any]],
    latest: Dict[UUID, Dict[str, Any]],
    provider: Any,
    *,
    window_days: int,
    spike_change_pct: float,
    with_trend: Any,
    latest_loader: Any,
) -> Dict[UUID, Dict[str, Any]]:
    """Fetch and persist snapshots for stale topics, returning updated latest snapshots."""
    fetched = []
    for t in stale:
        try:
            s = await provider.snapshot(t["external_id"], window_days)
        except ListeningUnavailable as e:
            logger.warning("Couldn't refresh listening topic %s: %s", t["name"], e)
            continue
        s = await with_trend(s, t)
        change = s.get("volume_change_pct")
        fetched.append({
            **s,
            "topic_id": str(t["id"]),
            "is_spike": change is not None and change >= spike_change_pct,
        })
    if fetched:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO topic_snapshots (topic_id, window_days, positive_pct, neutral_pct, negative_pct, volume,
                                             volume_change_pct, is_spike, daily_volume, top_posts, is_mock)
                SELECT s.topic_id, $2, s.positive, s.neutral, s.negative, s.volume, s.volume_change_pct, s.is_spike,
                       ARRAY(SELECT jsonb_array_elements_text(s.daily_volume)::int), s.top_posts, $3
                FROM jsonb_to_recordset($1::jsonb) AS s(
                    topic_id uuid, positive numeric, neutral numeric, negative numeric, volume int,
                    volume_change_pct numeric, is_spike boolean, daily_volume jsonb, top_posts jsonb
                )
                """,
                json.dumps(fetched), window_days, provider.is_mock,
            )
        return await latest_loader([t["id"] for t in topics])
    return latest


def schedule_snapshot_refresh(
    topics: List[Dict[str, Any]],
    stale: List[Dict[str, Any]],
    latest: Dict[UUID, Dict[str, Any]],
    provider: Any,
    *,
    window_days: int,
    spike_change_pct: float,
    with_trend: Any,
    latest_loader: Any,
) -> None:
    """Refresh stale snapshots in the background so page loads return cached data immediately."""
    pending = [t for t in stale if t["id"] not in _refreshing_ids]
    if not pending:
        return
    ids = {t["id"] for t in pending}
    _refreshing_ids.update(ids)

    async def _run() -> None:
        try:
            await refresh_stale_snapshots(
                topics, pending, latest, provider,
                window_days=window_days, spike_change_pct=spike_change_pct,
                with_trend=with_trend, latest_loader=latest_loader,
            )
        except Exception as exc:
            logger.warning("Background public-mood snapshot refresh failed: %s", exc)
        finally:
            _refreshing_ids.difference_update(ids)

    task = asyncio.create_task(_run(), name="public-mood-snapshot-refresh")
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
