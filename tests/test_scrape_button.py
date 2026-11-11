"""The scrape button: scraping starts only from it, runs outside the server's event loop, and reports how it went."""

import asyncio
import os
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

# The router imports auth, which needs a signing secret; CI has no .env (same as tests/test_performance_period.py).
os.environ.setdefault("JWT_SECRET", "test-only-secret-for-unit-tests-0123456789")

from fastapi import BackgroundTasks, HTTPException

from services.compliance.analytics import public_mood_service as pms
from tools.compliance.analytics import listening_router as lr


def _done(code=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


class ButtonRouteTests(unittest.TestCase):
    def setUp(self):
        lr._scrape.update(state="idle", started_at=None, finished_at=None, error=None, collection=None)

    def test_a_click_starts_one_background_scrape_and_says_so(self):
        tasks = BackgroundTasks()
        out = asyncio.run(lr.trigger_live_scrape(tasks, uuid4()))
        self.assertEqual(out["status"], "started")
        self.assertEqual(len(tasks.tasks), 1)
        self.assertEqual(lr._scrape["state"], "running")

    def test_a_second_click_while_one_is_running_is_refused_not_queued(self):
        tasks = BackgroundTasks()
        asyncio.run(lr.trigger_live_scrape(tasks, uuid4()))
        again = BackgroundTasks()
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(lr.trigger_live_scrape(again, uuid4()))
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(again.tasks, [])

    def test_status_reports_the_state(self):
        lr._scrape.update(state="failed", error="boom")
        self.assertEqual(asyncio.run(lr.scrape_status(uuid4()))["error"], "boom")

    def test_only_the_button_route_starts_the_runner(self):
        starters = [r.path for r in lr.router.routes
                    if "_run_scrape_and_refresh" in getattr(getattr(r, "endpoint", None), "__code__", MagicMock(co_names=())).co_names]
        self.assertEqual(starters, ["/listening/scrape"])

    def test_nothing_in_the_server_code_can_start_a_scrape_on_its_own(self):
        """No schedule, startup task or page-view path imports the scraper: only the router's button runs it."""
        root = lr.ROOT_DIR
        # An import of the scraper, a call to it, or a command line that runs it (a mention in a message doesn't count).
        triggers = ("import scrape_x_playwright", "from scripts import scrape_x_playwright", "from scripts.scrape_x_playwright",
                    "build_live_x_listening_json(", "scripts.scrape_x_playwright\"")
        offenders = []
        files = [os.path.join(root, "server.py")]
        for folder in ("services", "tools", "mcp_tools", "models", "postgresql_db"):
            for dirpath, _, names in os.walk(os.path.join(root, folder)):
                files += [os.path.join(dirpath, n) for n in names if n.endswith(".py")]
        for path in files:
            if any(t in open(path, encoding="utf-8").read() for t in triggers):
                offenders.append(os.path.relpath(path, root))
        self.assertEqual(offenders, [os.path.join("tools", "compliance", "analytics", "listening_router.py")])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        lr._scrape.update(state="idle", started_at=None, finished_at=None, error=None, collection=None)
        self.calls = []

        def step(name):
            async def run(*a, **k):
                self.calls.append(name)
            return AsyncMock(side_effect=run)

        self.patches = [
            patch.object(lr.svc, "record_conversations", step("record")),
            patch.object(lr.svc, "list_topics", step("topics")),
            patch.object(lr.svc, "list_trend_opportunities", step("trends")),
            patch.object(lr.svc, "index_conversations", step("index")),
            patch.object(lr, "reset_provider_cache", lambda: self.calls.append("reset")),
            patch.object(lr, "_collection_summary", AsyncMock(return_value={"status": "SUCCESS", "items": 7})),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_good_scrape_records_then_refreshes_topics_and_trends_then_indexes(self):
        # Recording comes first because the topic trends count what has been collected; indexing is last (it is slow).
        with patch.object(lr, "_run_scraper_process", return_value=_done()):
            asyncio.run(lr._run_scrape_and_refresh())
        self.assertEqual(self.calls, ["reset", "record", "topics", "trends", "index"])
        self.assertEqual(lr._scrape["state"], "finished")
        self.assertEqual(lr._scrape["collection"]["items"], 7)
        self.assertIsNotNone(lr._scrape["finished_at"])

    def test_a_failing_scraper_is_reported_and_nothing_is_refreshed(self):
        with patch.object(lr, "_run_scraper_process", return_value=_done(1, err="Traceback\nNotImplementedError")):
            asyncio.run(lr._run_scrape_and_refresh())
        self.assertEqual(lr._scrape["state"], "failed")
        self.assertIn("NotImplementedError", lr._scrape["error"])
        self.assertEqual(self.calls, [])

    def test_a_scrape_that_runs_too_long_is_stopped_and_reported(self):
        with patch.object(lr, "_run_scraper_process", side_effect=subprocess.TimeoutExpired("x", 1)):
            asyncio.run(lr._run_scrape_and_refresh())
        self.assertEqual(lr._scrape["state"], "failed")
        self.assertIn("longer than", lr._scrape["error"])

    def test_a_refresh_error_after_a_good_scrape_is_still_reported_not_swallowed(self):
        with patch.object(lr, "_run_scraper_process", return_value=_done()), \
                patch.object(lr.svc, "list_topics", AsyncMock(side_effect=RuntimeError("db down"))):
            asyncio.run(lr._run_scrape_and_refresh())
        self.assertEqual(lr._scrape["state"], "failed")
        self.assertIn("db down", lr._scrape["error"])

    def test_the_button_can_be_used_again_after_a_failure(self):
        with patch.object(lr, "_run_scraper_process", return_value=_done(1, err="x")):
            asyncio.run(lr._run_scrape_and_refresh())
        tasks = BackgroundTasks()
        self.assertEqual(asyncio.run(lr.trigger_live_scrape(tasks, uuid4()))["status"], "started")


class OwnProcessTests(unittest.TestCase):
    def test_the_scraper_runs_in_its_own_python_process_from_the_repo_root(self):
        with patch.object(lr.subprocess, "run", return_value=_done()) as run:
            lr._run_scraper_process()
        args, kwargs = run.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "scripts.scrape_x_playwright"])
        self.assertEqual(kwargs["cwd"], str(lr.ROOT_DIR))
        self.assertTrue(os.path.isdir(os.path.join(kwargs["cwd"], "scripts")))
        self.assertEqual(kwargs["timeout"], lr.SCRAPE_TIMEOUT_SECONDS)

    def test_it_works_whatever_event_loop_the_server_uses(self):
        # Playwright fails on Windows' Selector loop (uvicorn --reload); a subprocess started from a thread doesn't care.
        if sys.platform != "win32":
            self.skipTest("Windows-only event loop policy")

        async def go():
            with patch.object(lr.subprocess, "run", return_value=_done()):
                return await asyncio.to_thread(lr._run_scraper_process)

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            self.assertEqual(asyncio.run(go()).returncode, 0)
        finally:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


class NothingElseIndexesOnItsOwnTests(unittest.TestCase):
    def test_syncing_topics_no_longer_starts_conversation_indexing(self):
        soon = MagicMock()
        with patch.object(pms.store, "is_enabled", return_value=True), \
                patch.object(pms, "fetch_all", AsyncMock(return_value=[])), \
                patch.object(pms.store, "upsert", AsyncMock()), patch.object(pms.store, "delete", AsyncMock()), \
                patch.object(pms, "_index_conversations_soon", soon), \
                patch.object(pms, "_sync_conversation_vectors", AsyncMock()) as direct:
            asyncio.run(pms._sync_topic_vectors())
        soon.assert_not_called()
        direct.assert_not_called()

    def test_sync_conversations_is_the_explicit_step(self):
        with patch.object(pms, "_sync_conversation_vectors", AsyncMock()) as direct:
            asyncio.run(pms.sync_conversations())
        direct.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()


class FeedLocationTests(unittest.TestCase):
    def test_the_feed_lives_in_the_repo_data_folder_next_to_the_apps_other_data(self):
        from services.compliance.analytics.listening_providers import DEFAULT_X_JSON_PATH
        self.assertEqual(DEFAULT_X_JSON_PATH, lr.ROOT_DIR / ".data" / "x_listening_feed.json")
        self.assertTrue((lr.ROOT_DIR / "server.py").exists())  # ROOT_DIR really is the repo root

    def test_the_scraper_and_the_app_share_one_way_of_storing_and_loading_the_feed(self):
        from scripts import scrape_x_playwright as sx
        from services.compliance.analytics import listening_providers as lp
        self.assertIs(sx.save_feed, lp.save_feed)  # the scraper doesn't know a path of its own
        with patch.dict(os.environ, {"X_LISTENING_JSON_PATH": ""}):
            self.assertIsNone(lp.XJsonListeningProvider().json_path)  # None = the default source (database, else file)
