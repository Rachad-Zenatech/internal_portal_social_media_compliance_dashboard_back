"""Verify non-blocking snapshot refresh on /public-mood page load and feed cache invalidation."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.compliance.analytics import listening_providers, public_mood_service, public_mood_snapshots


class FastPublicMoodTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_snapshots_non_force_returns_latest_and_schedules_bg(self):
        tid = uuid4()
        topics = [{"id": tid, "external_id": "t1", "name": "Topic 1", "is_watched": False}]
        stale_ts = datetime.now(timezone.utc) - timedelta(days=2)
        latest = {tid: {"topic_id": tid, "fetched_at": stale_ts, "positive_pct": 50, "neutral_pct": 30, "negative_pct": 20, "volume": 10}}
        provider = MagicMock()
        provider.feed_stamp = AsyncMock(return_value=datetime.now(timezone.utc))
        provider.snapshot = AsyncMock()

        with patch.object(public_mood_service, "_latest_snapshots", AsyncMock(return_value=latest)), \
             patch.object(public_mood_service, "get_provider", return_value=provider), \
             patch.object(public_mood_snapshots, "schedule_snapshot_refresh") as sched:
            res = await public_mood_service._fresh_snapshots(topics, force=False)
            self.assertEqual(res, latest)
            sched.assert_called_once()
            provider.snapshot.assert_not_called()

    async def test_load_feed_caches_briefly_and_invalidates_on_save(self):
        store = MagicMock()
        store.is_enabled.return_value = True
        store.collection_name.return_value = "x_feed"
        store.read_feed = AsyncMock(return_value={"topics": [{"external_id": "1"}]})
        store.write_feed = AsyncMock()

        listening_providers._FEED_CACHE = None
        with patch.object(listening_providers, "_x_feed_store", return_value=store):
            a = await listening_providers.load_feed()
            b = await listening_providers.load_feed()
            self.assertEqual(a, b)
            self.assertEqual(store.read_feed.await_count, 1)
            await listening_providers.save_feed({"topics": []})
            await listening_providers.load_feed()
            self.assertEqual(store.read_feed.await_count, 2)
