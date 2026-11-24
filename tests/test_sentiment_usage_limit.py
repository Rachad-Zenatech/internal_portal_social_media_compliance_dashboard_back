"""The displayed sentiment allowance must not be cut down by a lower hidden scope limit."""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.ai.observability import ai_usage_service as usage


class SentimentLimitTests(unittest.TestCase):
    def test_all_daily_caps_default_to_unlimited(self):
        with patch.dict(os.environ, {}, clear=True):
            for scope in ("PER_USER_DAY", "PER_SUBMISSION_DAY", "GLOBAL_DAY"):
                self.assertEqual(usage.limit("SENTIMENT", scope), 0)
            self.assertEqual(usage.limit("PUBLIC_MOOD", "PER_SUBMISSION_DAY"), 0)
            self.assertEqual(usage.per_user_minute(), 0)

    def test_environment_overrides_still_apply(self):
        with patch.dict(os.environ, {"AI_LIMIT_SENTIMENT_PER_SUBMISSION_DAY": "25"}):
            self.assertEqual(usage.limit("SENTIMENT", "PER_SUBMISSION_DAY"), 25)


class SentimentUsageDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncapped_usage_returns_zero_limit_and_none_remaining(self):
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = object()
        count = AsyncMock(return_value=usage.Usage(used=3, limit=0, resets_at=None))
        with patch.dict(os.environ, {}, clear=True), patch.object(usage, "get_pool", return_value=pool), \
             patch.object(usage, "_count", count):
            result = (await usage.usage_for(uuid4(), uuid4()))["SENTIMENT"]
        self.assertEqual(result["used"], 3)
        self.assertEqual(result["limit"], 0)
        self.assertEqual(result["company_limit"], 0)
        self.assertIsNone(result["remaining"])
