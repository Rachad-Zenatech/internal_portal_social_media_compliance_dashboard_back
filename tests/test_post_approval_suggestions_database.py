"""Exercise late reviewer comments against real row locks and saved approvals."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
from fastapi import HTTPException

from services.compliance.submissions import post_approval_suggestions as policy
from services.compliance.submissions import review_comment_writes as comments
from services.compliance.submissions import submission_service as subs


@unittest.skipUnless(os.getenv("COMPLIANCE_SNAPSHOT_TEST_DSN"), "Requires a disposable test database")
class SuggestionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "suggestion_test_" + uuid4().hex
        self.admin = await asyncpg.connect(os.environ["COMPLIANCE_SNAPSHOT_TEST_DSN"])
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(os.environ["COMPLIANCE_SNAPSHOT_TEST_DSN"], min_size=1, max_size=3,
                                              server_settings={"search_path": self.schema})
        self.post_id, self.creator, self.approver, self.other = [uuid4() for _ in range(4)]
        self.deadline = datetime.now(timezone.utc) + timedelta(hours=1)
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE content_submissions (
                    id UUID PRIMARY KEY, status TEXT, current_version INT, created_by UUID,
                    approval_needed_by TIMESTAMPTZ, deleted_at TIMESTAMPTZ
                );
                CREATE TABLE content_review_jobs (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), submission_id UUID,
                    version_number INT, decision TEXT, decided_by UUID, decided_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE content_review_comments (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), submission_id UUID,
                    body TEXT, resolved BOOLEAN DEFAULT false
                );
            """)
            await conn.execute("INSERT INTO content_submissions VALUES ($1,'APPROVED',2,$2,$3,NULL)", self.post_id, self.creator, self.deadline)
            await conn.execute("INSERT INTO content_review_jobs(submission_id,version_number,decision,decided_by) VALUES ($1,2,'APPROVE',$2)", self.post_id, self.approver)
        self.patches = [patch.object(comments, "get_pool", return_value=self.pool),
                        patch.object(subs, "_can_suggest", new=AsyncMock(return_value=True)),
                        patch.object(policy, "fetch_all", new=self.read)]
        for entry in self.patches:
            entry.start()

    async def asyncTearDown(self):
        for entry in reversed(self.patches):
            entry.stop()
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def read(self, query, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def insert(self, user_id=None):
        return await comments.write(self.post_id, user_id or self.other,
                                    "INSERT INTO content_review_comments(submission_id,body) VALUES ($1,'A suggestion') RETURNING id",
                                    self.post_id)

    async def close_window(self):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE content_submissions SET approval_needed_by=NOW()-INTERVAL '1 second'")

    async def test_another_reviewer_can_add_suggestions_without_changing_approval(self):
        saved = await self.insert()
        self.assertIsNotNone(saved["id"])
        row = (await self.read("SELECT * FROM content_submissions"))[0]
        self.assertEqual(row["status"], "APPROVED")
        self.assertEqual((await self.read("SELECT decided_by FROM content_review_jobs"))[0]["decided_by"], self.approver)

    async def test_closed_window_blocks_add_resolve_and_delete_even_from_stale_tabs(self):
        saved = await self.insert()
        await self.close_window()
        mutations = [
            ("INSERT INTO content_review_comments(submission_id,body) VALUES ($1,'Late') RETURNING id", self.post_id),
            ("UPDATE content_review_comments SET resolved=true WHERE id=$1 RETURNING id", saved["id"]),
            ("DELETE FROM content_review_comments WHERE id=$1 RETURNING id", saved["id"]),
        ]
        for query, key in mutations:
            with self.subTest(query=query), self.assertRaises(HTTPException) as error:
                await comments.write(self.post_id, self.other, query, key)
            self.assertEqual(error.exception.status_code, 403)
        notes = await self.read("SELECT * FROM content_review_comments")
        self.assertEqual(len(notes), 1)
        self.assertFalse(notes[0]["resolved"])

    async def test_creator_and_original_approver_keep_existing_comment_permissions(self):
        await self.close_window()
        await self.insert(self.creator)
        await self.insert(self.approver)
        self.assertEqual(len(await self.read("SELECT * FROM content_review_comments")), 2)

    async def test_queue_permissions_preserve_approval_but_remove_other_reviewer_actions(self):
        row = dict((await self.read("SELECT * FROM content_submissions"))[0])
        target = SimpleNamespace(can_manage_approval=True)
        await policy.enrich([target], [row], self.other)
        self.assertTrue(target.can_suggest_after_approval)
        self.assertFalse(target.can_manage_approval)
        self.assertEqual(target.suggestions_until, self.deadline)

    async def test_approval_from_an_older_version_does_not_authorize_the_current_version(self):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE content_review_jobs SET version_number=1")
        row = dict((await self.read("SELECT * FROM content_submissions"))[0])
        self.assertEqual(await policy.approvals([row]), {})
        with self.assertRaises(HTTPException):
            await self.insert()

    async def test_publishing_state_uses_current_server_deadline(self):
        row = dict((await self.read("SELECT * FROM content_submissions"))[0])
        self.assertFalse(policy.publishing_access(row).can_publish)
        await self.close_window()
        row = dict((await self.read("SELECT * FROM content_submissions"))[0])
        self.assertTrue(policy.publishing_access(row).can_publish)


if __name__ == "__main__":
    unittest.main()
