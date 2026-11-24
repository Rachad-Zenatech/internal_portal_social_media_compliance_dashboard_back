"""Reproduce the production foreign-key failure in a disposable PostgreSQL database."""

import json
import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
from fastapi import HTTPException

from services.compliance.submissions import draft_deletion, submission_service as subs


@unittest.skipUnless(os.getenv("COMPLIANCE_DRAFT_TEST_DSN"), "Requires a dedicated draft deletion test database")
class DraftDeletionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "draft_deletion_" + uuid4().hex
        self.conn = await asyncpg.connect(os.environ["COMPLIANCE_DRAFT_TEST_DSN"])
        await self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        await self.conn.execute(f'SET search_path TO "{self.schema}"')
        await self.conn.execute("""
            CREATE TABLE content_submissions (
                id UUID PRIMARY KEY, status TEXT DEFAULT 'DRAFT', current_version INT DEFAULT 0,
                script_status TEXT DEFAULT 'APPROVED', script_send JSONB,
                deleted_at TIMESTAMPTZ, deleted_by UUID, updated_at TIMESTAMPTZ
            );
            CREATE TABLE script_document_posts (
                document_id UUID PRIMARY KEY, body TEXT,
                submission_id UUID REFERENCES content_submissions(id)
            );
            CREATE TABLE content_script_batch_items (
                id UUID PRIMARY KEY, body TEXT,
                submission_id UUID REFERENCES content_submissions(id)
            );
            CREATE TABLE content_submission_files (
                id UUID PRIMARY KEY, submission_id UUID REFERENCES content_submissions(id) ON DELETE CASCADE
            );
            CREATE TABLE compliance_check_snapshots (
                id UUID PRIMARY KEY, submission_id UUID, post_details JSONB, check_result JSONB
            );
        """)
        self.post, self.user = uuid4(), uuid4()
        await self.conn.execute("INSERT INTO content_submissions(id) VALUES ($1)", self.post)
        await self.conn.execute("INSERT INTO script_document_posts VALUES ($1,'Approved source script',$2)", uuid4(), self.post)
        await self.conn.execute("INSERT INTO content_script_batch_items VALUES ($1,'Approved batch script',$2)", uuid4(), self.post)
        await self.conn.execute("INSERT INTO content_submission_files VALUES ($1,$2)", uuid4(), self.post)
        await self.conn.execute("INSERT INTO compliance_check_snapshots VALUES ($1,$2,$3::jsonb,$4::jsonb)",
                                uuid4(), self.post, json.dumps({"title": "Test draft"}), json.dumps({"score": 85}))

    async def asyncTearDown(self):
        await self.conn.execute('SET search_path TO public')
        await self.conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.conn.close()

    async def delete(self):
        async with self.conn.transaction():
            return await draft_deletion.delete_record(self.conn, self.post, self.user)

    async def test_reproduces_fk_error_then_deletes_draft_and_preserves_script_and_snapshot(self):
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            async with self.conn.transaction():
                await self.conn.execute("DELETE FROM content_submissions WHERE id=$1", self.post)
        self.assertFalse(await self.delete())
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM content_submissions"), 0)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM content_submission_files"), 0)
        for table in ("script_document_posts", "content_script_batch_items"):
            row = await self.conn.fetchrow(f"SELECT * FROM {table}")
            self.assertIsNone(row["submission_id"])
            self.assertTrue(row["body"].startswith("Approved"))
        snapshot = await self.conn.fetchrow("SELECT * FROM compliance_check_snapshots")
        self.assertEqual(snapshot["submission_id"], self.post)
        self.assertEqual(json.loads(snapshot["check_result"])["score"], 85)

    async def test_all_link_changes_roll_back_if_deletion_fails(self):
        await self.conn.execute("CREATE TABLE other_reference (submission_id UUID REFERENCES content_submissions(id))")
        await self.conn.execute("INSERT INTO other_reference VALUES ($1)", self.post)
        with self.assertRaises(asyncpg.ForeignKeyViolationError):
            await self.delete()
        for table in ("script_document_posts", "content_script_batch_items"):
            self.assertEqual(await self.conn.fetchval(f"SELECT submission_id FROM {table}"), self.post)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM content_submissions"), 1)

    async def test_submitted_history_keeps_existing_soft_delete_behavior(self):
        await self.conn.execute("UPDATE content_submissions SET current_version=1 WHERE id=$1", self.post)
        with patch.object(subs, "_add_event", new=AsyncMock()) as event:
            self.assertTrue(await self.delete())
        self.assertIsNotNone(await self.conn.fetchval("SELECT deleted_at FROM content_submissions"))
        self.assertEqual(await self.conn.fetchval("SELECT submission_id FROM script_document_posts"), self.post)
        event.assert_awaited_once()

    async def test_rechecks_script_status_under_the_post_lock(self):
        await self.conn.execute("UPDATE content_submissions SET script_status='IN_REVIEW' WHERE id=$1", self.post)
        with self.assertRaises(HTTPException) as error:
            await self.delete()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM content_submissions"), 1)

    async def test_rechecks_pending_script_send_under_the_post_lock(self):
        await self.conn.execute("UPDATE content_submissions SET script_send=$2::jsonb WHERE id=$1", self.post,
                                json.dumps({"status": "PROCESSING"}))
        with patch.object(subs, "_script_with_reviewer", return_value=True), self.assertRaises(HTTPException):
            await self.delete()

    async def test_repeated_delete_is_a_conflict_instead_of_an_unhandled_error(self):
        await self.delete()
        with self.assertRaises(HTTPException) as error:
            await self.delete()
        self.assertEqual(error.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
