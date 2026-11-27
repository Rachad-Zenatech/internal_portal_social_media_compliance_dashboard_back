"""A draft autosave cannot land after the post has been sent for review."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import HTTPException
from services.compliance.submissions.creator_write_guard import lock_editable


class CreatorWriteGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_rechecks_current_status_under_row_lock(self):
        for status in ("IN_REVIEW", "APPROVED", "REJECTED"):
            with self.subTest(status=status):
                conn = SimpleNamespace(fetchrow=AsyncMock(return_value={"status": status}))
                with self.assertRaises(HTTPException) as error:
                    await lock_editable(conn, "post")
                self.assertEqual(error.exception.status_code, 409)
                self.assertIn("FOR UPDATE", conn.fetchrow.call_args.args[0])

    async def test_drafts_and_returned_posts_remain_editable(self):
        for status in ("DRAFT", "CHANGES_REQUESTED"):
            conn = SimpleNamespace(fetchrow=AsyncMock(return_value={"status": status}))
            await lock_editable(conn, "post")

    async def test_deleted_posts_cannot_be_saved(self):
        conn = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
        with self.assertRaises(HTTPException) as error:
            await lock_editable(conn, "post")
        self.assertEqual(error.exception.status_code, 404)
