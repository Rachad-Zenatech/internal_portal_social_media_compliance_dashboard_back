"""Only actual media revisions invalidate saved analyses."""
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from uuid import uuid4
from services.compliance.sentiment import jobs

from services.compliance.submissions import manual_check_service as manual, media_freshness as media
from services.compliance.submissions import post_steps_service
from services.compliance.evaluation import script_review_service as scripts
from tests.test_manual_checks import NOW, checked_row
from tests.test_post_steps import post


class MediaFreshness(unittest.TestCase):
    def test_detail_edits_reuse_existing_manual_check(self):
        row = dict(checked_row(), media_edited_at=NOW, content_edited_at=NOW + timedelta(hours=1),
                   title="New title", caption_text="Updated caption", brand_codes=["NEW"])
        result, _ = manual.require_current_check(row, "v1")
        self.assertEqual(result.score, 95)

    def test_changed_or_removed_media_requires_rerun(self):
        for marker in (None, NOW.isoformat()):
            row = dict(checked_row(checked_media_edited_at=marker), media_edited_at=NOW + timedelta(seconds=1))
            self.assertFalse(manual.content_is_current(row, row["latest_check"]))

    def test_legacy_result_uses_media_time_even_after_details_changed(self):
        row = dict(checked_row(), media_edited_at=NOW - timedelta(minutes=1),
                   content_edited_at=NOW + timedelta(hours=1))
        self.assertTrue(manual.content_is_current(row, row["latest_check"]))
        row["media_edited_at"] = NOW + timedelta(seconds=1)
        self.assertFalse(manual.content_is_current(row, row["latest_check"]))

    def test_new_marker_keeps_inflight_media_edit_stale(self):
        row = dict(checked_row(checked_media_edited_at=NOW.isoformat(),
                              checked_at=(NOW + timedelta(minutes=1)).isoformat()),
                   media_edited_at=NOW + timedelta(seconds=1))
        self.assertFalse(manual.content_is_current(row, row["latest_check"]))

    def test_no_media_marker_stays_current_until_media_added(self):
        row = dict(checked_row(checked_media_edited_at=None), media_edited_at=None)
        self.assertTrue(manual.content_is_current(row, row["latest_check"]))
        row["media_edited_at"] = NOW
        self.assertFalse(manual.content_is_current(row, row["latest_check"]))

    def test_progress_and_revision_keep_checks_after_detail_edits(self):
        for status in ("DRAFT", "CHANGES_REQUESTED"):
            row = post(status=status, media_edited_at=NOW - timedelta(hours=2),
                       content_edited_at=NOW + timedelta(minutes=1), revision_at=NOW,
                       unresolved_comments=False, check_at=NOW - timedelta(hours=1),
                       sentiment_at=NOW - timedelta(hours=1))
            steps = post_steps_service.compute_steps(row)
            self.assertEqual((steps.compliance, steps.sentiment), ("done", "done"))
            row["media_edited_at"] = NOW
            steps = post_steps_service.compute_steps(row)
            self.assertEqual((steps.compliance, steps.sentiment), ("stale", "stale"))

    def test_script_checks_ignore_details_but_detect_wording_changes(self):
        check = dict(score=100, rulebook_version="v1")
        stored = scripts.precheck_record({}, "Original script", check)
        row = dict(title="Changed", platforms=["TIKTOK"], media_edited_at=NOW)
        self.assertEqual(scripts.still_valid(stored, row, "Original script", "v1"), check)
        self.assertIsNone(scripts.still_valid(stored, row, "Revised script", "v1"))
        self.assertIsNone(scripts.still_valid(stored, row, "Original script", "v2"))

    def test_revision_sql_excludes_supporting_files_and_extraction_updates(self):
        self.assertIn("'MAIN', 'IMAGE', 'SCRIPT'", media.REVISION_SQL)
        self.assertNotIn("SUPPORTING", media.REVISION_SQL)
        self.assertIn("deleted_at", media.REVISION_SQL)
        self.assertNotIn("transcript_updated_at", media.REVISION_SQL)


class InflightCheck(unittest.IsolatedAsyncioTestCase):
    async def test_details_during_check_are_allowed_media_or_workflow_changes_are_not(self):
        row = dict(checked_row(), status="DRAFT", current_version=1, media_edited_at=NOW)
        current = dict(row, content_edited_at=NOW + timedelta(seconds=1))
        conn = AsyncMock()
        conn.fetchrow.return_value = current
        self.assertTrue(await media.check_still_current(conn, row, NOW))
        for field, value in (("media_edited_at", NOW + timedelta(seconds=1)),
                             ("status", "IN_REVIEW"), ("current_version", 2)):
            conn.fetchrow.return_value = dict(current, **{field: value})
            self.assertFalse(await media.check_still_current(conn, row, NOW))


class SentimentFreshness(unittest.IsolatedAsyncioTestCase):
    async def test_detail_hash_does_not_invalidate_sentiment_but_media_changes_do(self):
        for edited, expected in ((NOW - timedelta(seconds=1), True), (NOW + timedelta(seconds=1), False)):
            row = dict(status="COMPLETED", trigger="MANUAL", created_at=NOW)
            model = SimpleNamespace(is_current=True)
            with patch.object(jobs, "fetch_one", AsyncMock(side_effect=[row, {"media_edited_at": edited}])), \
                 patch.object(jobs, "_to_model", return_value=model), \
                 patch.object(jobs, "_with_feedback", AsyncMock(return_value=model)), \
                 patch.object(jobs, "_with_comparison", AsyncMock(return_value=model)):
                result = await jobs.get_latest(uuid4())
            self.assertEqual(result.is_current, expected)
