"""Regressions for SUB-1219's schedule-only invalidation and cached sentiment reuse.

Database checks require a disposable database named freshness_test via
FRESHNESS_TEST_DATABASE_URL. No model or other external service is called.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncpg
from services.compliance.submissions.submission_content_revision import content_changed, same_value
from services.compliance.sentiment import jobs, reuse_freshness as reuse

TEST_URL = os.getenv("FRESHNESS_TEST_DATABASE_URL")


class RevisionTests(unittest.TestCase):
    def test_sub1219_publish_date_change_is_not_a_content_revision(self):
        old = {"title": "Reviewed text post", "caption_text": "Saved reviewer wording.", "publish_at": None}
        saved = {**old, "publish_at": datetime.now(timezone.utc) + timedelta(days=1)}
        self.assertFalse(content_changed(old, saved))

    def test_reviewer_and_approval_date_changes_preserve_results(self):
        self.assertFalse(content_changed({}, {"approval_needed_by": datetime.now(timezone.utc),
                                             "reviewer_ids": [uuid4()], "reviewer_route": ["MARKETING"]}))

    def test_real_analysis_input_changes_still_invalidate_results(self):
        for key in ("caption_text", "title", "platform_captions", "region", "source_links", "rule_sets", "brand_codes"):
            with self.subTest(field=key):
                self.assertTrue(content_changed({key: "before"}, {key: "after"}))

    def test_equal_json_and_identifier_values_are_not_edits(self):
        identity = uuid4()
        self.assertTrue(same_value(str(identity), identity))
        self.assertTrue(same_value({"LINKEDIN": "Reviewed text"}, '{"LINKEDIN":"Reviewed text"}'))


@unittest.skipUnless(TEST_URL, "Set FRESHNESS_TEST_DATABASE_URL for database checks")
class ReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if urlparse(TEST_URL).path != "/freshness_test":
            raise RuntimeError("Refusing to reset any database other than freshness_test")
        self.pool = await asyncpg.create_pool(TEST_URL, min_size=1, max_size=2, command_timeout=10)
        self.addAsyncCleanup(self.pool.close)
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS content_submissions (id uuid PRIMARY KEY, content_type text,
                content_edited_at timestamptz, image_provenance_edited_at timestamptz, current_version integer);
            CREATE TABLE IF NOT EXISTS content_submission_files (submission_id uuid, kind text, created_at timestamptz, deleted_at timestamptz);
            CREATE TABLE IF NOT EXISTS content_script_versions (submission_id uuid, created_at timestamptz);
            CREATE TABLE IF NOT EXISTS content_sentiment_analyses (id uuid PRIMARY KEY, submission_id uuid,
                content_hash text, status text, input jsonb, result jsonb, created_at timestamptz);
            TRUNCATE content_submissions, content_submission_files, content_script_versions, content_sentiment_analyses;
        """)
        self.post_id, self.analysis_id = uuid4(), uuid4()
        self.edited = datetime.now(timezone.utc)
        self.input = {"caption": "Reviewed text.", "model": "test-model"}
        await self.pool.execute("INSERT INTO content_submissions VALUES ($1,'TEXT_POST',$2,NULL,2)", self.post_id, self.edited)
        await self.pool.execute("INSERT INTO content_sentiment_analyses VALUES ($1,$2,$3,'COMPLETED',$4::jsonb,$5::jsonb,$6)",
                                self.analysis_id, self.post_id, jobs._hash(self.input), json.dumps(self.input),
                                '{"summary":"Original result"}', self.edited - timedelta(minutes=1))
        self.row = await self.pool.fetchrow("SELECT * FROM content_submissions WHERE id=$1", self.post_id)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, name, value in ((reuse, "fetch_one", self.pool.fetchrow), (jobs, "fetch_one", self.pool.fetchrow),
                                    (jobs, "_build_input", AsyncMock(return_value=self.input)), (jobs, "schedule", MagicMock()),
                                    (jobs.ai_usage_service, "reserve", AsyncMock())):
            self.stack.enter_context(patch.object(target, name, value))

    async def analysis(self):
        return await self.pool.fetchrow("SELECT * FROM content_sentiment_analyses WHERE id=$1", self.analysis_id)

    async def test_cached_recheck_stays_current_without_another_model_run(self):
        before = await self.analysis()
        self.assertFalse(await reuse.is_current(before))
        identity = await jobs._queue_for(self.row, "MANUAL", uuid4())
        self.assertEqual(identity, self.analysis_id)
        after = await self.analysis()
        self.assertTrue(await reuse.is_current(after))
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(after["result"], before["result"])
        self.assertEqual(after["content_hash"], before["content_hash"])
        jobs.schedule.assert_not_called()
        jobs.ai_usage_service.reserve.assert_not_awaited()

    async def test_later_real_content_edit_invalidates_reused_result(self):
        await jobs._queue_for(self.row, "MANUAL", uuid4())
        await self.pool.execute("UPDATE content_submissions SET content_edited_at=$2 WHERE id=$1", self.post_id, self.edited + timedelta(seconds=1))
        self.assertFalse(await reuse.is_current(await self.analysis()))

    async def test_edit_during_input_validation_cannot_validate_old_results(self):
        async def change_during_build(row):
            await self.pool.execute("UPDATE content_submissions SET content_edited_at=$2 WHERE id=$1", self.post_id, self.edited + timedelta(seconds=1))
            return self.input
        with patch.object(jobs, "_build_input", change_during_build):
            await jobs._queue_for(self.row, "MANUAL", uuid4())
        self.assertFalse(await reuse.is_current(await self.analysis()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
