"""Schema updates finish before startup proceeds, including with multiple workers."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from services.shared.core import startup_service


class StartupSchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_setups_run_in_order_and_release_the_lock(self):
        conn = AsyncMock()
        conn.fetchval.side_effect = [False, True]
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        calls = AsyncMock()
        first = SimpleNamespace(setup=calls.first)
        second = SimpleNamespace(setup=calls.second)
        with (
            patch.object(startup_service, "SCHEMA_SETUPS", [("first", "First"), ("second", "Second")]),
            patch.object(startup_service.importlib, "import_module", side_effect=[first, second]),
            patch.object(startup_service.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            await startup_service.run_schema_setups(pool)
        self.assertEqual([call[0] for call in calls.mock_calls], ["first", "second"])
        sleep.assert_awaited_once_with(0.5)
        conn.execute.assert_awaited_once_with("SELECT pg_advisory_unlock(8009, 1)")

    async def test_failure_stops_startup_and_releases_the_lock(self):
        conn = AsyncMock()
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        broken = SimpleNamespace(setup=AsyncMock(side_effect=RuntimeError("schema failed")))
        with (
            patch.object(startup_service, "SCHEMA_SETUPS", [("broken", "Broken"), ("later", "Later")]),
            patch.object(startup_service.importlib, "import_module", return_value=broken) as load,
            self.assertRaisesRegex(RuntimeError, "schema failed"),
        ):
            await startup_service.run_schema_setups(pool)
        load.assert_called_once_with("postgresql_db.schemas.broken")
        conn.execute.assert_awaited_once_with("SELECT pg_advisory_unlock(8009, 1)")

    async def test_workers_do_not_run_setups_concurrently(self):
        lock = asyncio.Lock()
        entered = asyncio.Event()
        release = asyncio.Event()
        runs = 0

        async def try_lock(*args):
            if lock.locked():
                return False
            await lock.acquire()
            return True

        async def unlock(*args):
            lock.release()

        async def setup():
            nonlocal runs
            runs += 1
            entered.set()
            await release.wait()

        conn = AsyncMock()
        conn.fetchval.side_effect = try_lock
        conn.execute.side_effect = unlock
        pool = MagicMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        with (
            patch.object(startup_service, "SCHEMA_SETUPS", [("schema", "Schema")]),
            patch.object(startup_service.importlib, "import_module", return_value=SimpleNamespace(setup=setup)),
        ):
            first = asyncio.create_task(startup_service.run_schema_setups(pool))
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = asyncio.create_task(startup_service.run_schema_setups(pool))
            await asyncio.sleep(0)
            self.assertEqual(runs, 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
        self.assertEqual(runs, 2)
        self.assertFalse(lock.locked())
