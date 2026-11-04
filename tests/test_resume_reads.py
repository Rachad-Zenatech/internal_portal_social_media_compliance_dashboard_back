import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.compliance import document_text_service as docs
from services.compliance import image_text_service as images


class ResumeInterruptedReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_images_cut_off_by_a_restart_are_read_again(self):
        ids = [uuid4(), uuid4()]
        schedule = MagicMock()
        fetch = AsyncMock(return_value=[{"id": i} for i in ids])
        with patch.object(images, "fetch_all", new=fetch), patch.object(images, "schedule", new=schedule):
            self.assertEqual(await images.resume_interrupted(), 2)
        sql = fetch.await_args.args[0]
        self.assertIn("image_text_status = NULL", sql)
        self.assertIn("RETURNING id", sql)  # one statement picks them, so only one process takes each
        self.assertIn(f"INTERVAL '{images.RESUME_AFTER_MINUTES} minutes'", sql)  # a read just started elsewhere is left alone
        self.assertEqual(list(schedule.call_args.args[0]), ids)

    async def test_documents_cut_off_by_a_restart_are_read_again(self):
        ids = [uuid4()]
        schedule = MagicMock()
        fetch = AsyncMock(return_value=[{"id": i} for i in ids])
        with patch.object(docs, "fetch_all", new=fetch), patch.object(docs, "schedule", new=schedule):
            self.assertEqual(await docs.resume_interrupted(), 1)
        self.assertIn("doc_text_status = NULL", fetch.await_args.args[0])
        self.assertEqual(list(schedule.call_args.args[0]), ids)

    async def test_nothing_to_resume_is_quiet(self):
        with patch.object(images, "fetch_all", new=AsyncMock(return_value=[])), patch.object(images, "schedule", new=MagicMock()):
            self.assertEqual(await images.resume_interrupted(), 0)


class StuckReadRetriedTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_stuck_reads_older_than_the_limit_are_retried(self):
        fetch = AsyncMock(return_value=[])
        with patch.object(images, "fetch_all", new=fetch), patch.object(images, "schedule", new=MagicMock()):
            await images.schedule_missing([uuid4()])
        sql = fetch.await_args.args[0]
        self.assertIn("image_text_status = 'PROCESSING' AND image_text_updated_at < NOW()", sql)


if __name__ == "__main__":
    unittest.main()
