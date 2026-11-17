import asyncio
import unittest

from services.compliance.submissions.request_coalescer import ShortLivedRequestCache


class ShortLivedRequestCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_reads_share_one_loader(self):
        cache = ShortLivedRequestCache()
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def load():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return ["queue"]

        first = asyncio.create_task(cache.get_or_load("queue:user:review", load))
        await started.wait()
        second = asyncio.create_task(cache.get_or_load("queue:user:review", load))
        release.set()

        self.assertEqual(await asyncio.gather(first, second), [["queue"], ["queue"]])
        self.assertEqual(calls, 1)

    async def test_completed_result_is_reused_briefly(self):
        cache = ShortLivedRequestCache()
        calls = 0

        async def load():
            nonlocal calls
            calls += 1
            return {"review": 3}

        await cache.get_or_load("queue-counts:user:all", load)
        self.assertEqual(await cache.get_or_load("queue-counts:user:all", load), {"review": 3})
        self.assertEqual(calls, 1)
