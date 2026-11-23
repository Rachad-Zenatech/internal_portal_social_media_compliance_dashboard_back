"""A manual stop cancels work, survives polling and allows an explicit fresh run."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException
from services.compliance.sentiment import cancellation, jobs
from services.compliance.submissions import submission_service
from services.compliance.submissions.disconnected_analysis import run_until_disconnected


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_active_compliance_work(self):
        ended = asyncio.Event()

        async def work():
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
        with self.assertRaises(asyncio.CancelledError):
            await run_until_disconnected(request, work())
        self.assertTrue(ended.is_set())

    async def test_normal_completion_keeps_result(self):
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        self.assertEqual(await run_until_disconnected(request, AsyncMock(return_value=95)()), 95)

    async def test_other_worker_observes_persisted_stop_and_cancels_ai(self):
        ended = asyncio.Event()

        async def work():
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        with patch.object(cancellation, "fetch_one", AsyncMock(return_value={"status": "FAILED"})), \
             patch.object(cancellation.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(asyncio.CancelledError):
                await cancellation.run_cancellable(uuid4(), work())
        self.assertTrue(ended.is_set())

    async def test_owner_stop_persists_status_and_cancels_local_task(self):
        analysis_id, submission_id, user_id = uuid4(), uuid4(), uuid4()
        task = asyncio.create_task(asyncio.Event().wait(), name=f"sentiment-{analysis_id}")
        await asyncio.sleep(0)
        stopped = SimpleNamespace(status="FAILED", error=cancellation.STOPPED_MESSAGE)
        save = AsyncMock(return_value={"id": analysis_id})
        with patch.object(submission_service, "_get_owned_row", AsyncMock(return_value={"status": "DRAFT"})), \
             patch.object(cancellation, "fetch_one", save), \
             patch.object(jobs, "get_analysis", AsyncMock(return_value=stopped)), \
             patch("services.compliance.sentiment.config._tasks", {task}):
            result = await cancellation.stop_analysis(submission_id, analysis_id, user_id)
        self.assertIs(result, stopped)
        self.assertTrue(task.cancelled())
        self.assertIn("status = 'FAILED'", save.call_args.args[0])
        self.assertIn("retry_at = NULL", save.call_args.args[0])
        self.assertEqual(save.call_args.args[1:], (analysis_id, submission_id, cancellation.STOPPED_MESSAGE))

    async def test_stop_denies_unauthorized_access_before_writing(self):
        with patch.object(submission_service, "_get_owned_row", AsyncMock(side_effect=HTTPException(404))), \
             patch.object(cancellation, "fetch_one", AsyncMock()) as save:
            with self.assertRaises(HTTPException):
                await cancellation.stop_analysis(uuid4(), uuid4(), uuid4())
        save.assert_not_awaited()

    async def test_clicking_run_after_stop_creates_and_schedules_fresh_analysis(self):
        old_id, new_id, submission_id = uuid4(), uuid4(), uuid4()
        stopped = {"id": old_id, "status": "FAILED"}

        async def database(sql, *args):
            if "SELECT id FROM content_sentiment_analyses" in sql:
                self.assertIn("status IN ('PENDING', 'PROCESSING', 'COMPLETED')", sql)
                return None if stopped["status"] == "FAILED" else stopped
            return {"id": new_id}

        with patch.object(jobs, "_build_input", AsyncMock(return_value={"caption": "post"})), \
             patch.object(jobs, "fetch_one", side_effect=database), \
             patch.object(jobs.ai_usage_service, "reserve", AsyncMock()), \
             patch.object(jobs, "schedule") as schedule:
            result = await jobs._queue_for({"id": submission_id, "current_version": 1}, "MANUAL", uuid4())
        self.assertEqual(result, new_id)
        schedule.assert_called_once_with([new_id])

    async def test_cancelled_waiting_analysis_cannot_be_resumed_by_transcript_completion(self):
        analysis_id = uuid4()
        with patch.object(jobs, "_build_input", AsyncMock(return_value={})), \
             patch.object(jobs, "fetch_one", AsyncMock(side_effect=[None, None])) as fetch, \
             patch.object(jobs, "schedule") as schedule:
            result = await jobs._queue_for({"id": uuid4(), "current_version": 1}, "MANUAL", uuid4(), analysis_id)
        self.assertEqual(result, analysis_id)
        self.assertIn("WHERE content_sentiment_analyses.status = 'WAITING_TRANSCRIPT'", fetch.call_args.args[0])
        schedule.assert_not_called()

    async def test_late_completion_cannot_overwrite_stopped_analysis_or_emit_success_event(self):
        conn = MagicMock()
        conn.execute = AsyncMock(return_value="UPDATE 0")
        conn.transaction.return_value.__aenter__ = AsyncMock()
        conn.transaction.return_value.__aexit__ = AsyncMock()
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock()
        result = SimpleNamespace(model_dump_json=lambda: "{}")
        with patch.object(jobs, "get_pool", return_value=pool):
            await jobs._finish(uuid4(), uuid4(), uuid4(), result, None)
        self.assertEqual(conn.execute.await_count, 1)
        self.assertIn("AND status = 'PROCESSING'", conn.execute.call_args.args[0])

    async def test_polling_cannot_restart_a_stopped_processing_row(self):
        analysis_id = uuid4()
        stale = {"id": analysis_id, "status": "PROCESSING", "attempts": 1}
        with patch.object(jobs, "fetch_all", AsyncMock(return_value=[stale])), \
             patch.object(jobs, "fetch_one", AsyncMock(return_value=None)) as update, \
             patch.object(jobs, "schedule"):
            await jobs.resume_unfinished(uuid4())
        self.assertIn("AND status = 'PROCESSING'", update.call_args.args[0])
