"""Edits and media completion cannot start analyses; submission reuses a current manual check."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from services.compliance.submissions import manual_check_service as manual
from services.compliance.submissions import submission_service as submissions
from services.compliance.sentiment import jobs

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def checked_row(**overrides):
    check = dict(is_compliant=True, score=95, rulebook_version="v1", checked_at=NOW.isoformat(),
                 manual_requested_at=NOW.isoformat(), checked_content_edited_at=NOW.isoformat(),
                 analysis_pending=False)
    check.update(overrides)
    return dict(id=uuid4(), content_edited_at=NOW, latest_check=check)


class SavedResults(unittest.TestCase):
    def test_submit_reuses_current_result(self):
        row = checked_row()
        result, saved = manual.require_current_check(row, "v1")
        self.assertEqual(result.score, 95)
        self.assertIs(saved, row["latest_check"])

    def test_missing_edited_or_old_rulebook_requires_manual_run(self):
        for row, version in ((dict(latest_check=None), "v1"), (checked_row(), "v2"),
                             ({**checked_row(), "content_edited_at": NOW + timedelta(seconds=1)}, "v1")):
            with self.subTest(row=row, version=version), self.assertRaises(HTTPException) as error:
                manual.require_current_check(row, version)
            self.assertEqual(error.exception.status_code, 422)

    def test_inflight_edit_does_not_become_current_when_check_finishes(self):
        row = checked_row(checked_at=(NOW + timedelta(minutes=1)).isoformat())
        row["content_edited_at"] = NOW + timedelta(seconds=30)
        self.assertFalse(manual.content_is_current(row, row["latest_check"]))

    def test_pending_or_unavailable_results_cannot_be_submitted(self):
        for flags in (dict(analysis_pending=True), dict(ai_unavailable=True)):
            with self.subTest(flags=flags), self.assertRaises(HTTPException):
                manual.require_current_check(checked_row(**flags), "v1")

    def test_only_current_pending_manual_request_can_continue(self):
        self.assertTrue(manual.may_finish_check(checked_row(analysis_pending=True)))
        self.assertFalse(manual.may_finish_check(checked_row()))
        self.assertFalse(manual.may_finish_check(checked_row(analysis_pending=True, manual_requested_at=None)))
        row = checked_row(analysis_pending=True)
        row["content_edited_at"] = NOW + timedelta(seconds=1)
        self.assertFalse(manual.may_finish_check(row))

    def test_json_storage_and_older_results_are_supported(self):
        row = checked_row()
        row["latest_check"].pop("checked_content_edited_at")
        row["latest_check"] = json.dumps(row["latest_check"])
        result, _ = manual.require_current_check(row, "v1")
        self.assertEqual(result.score, 95)


class NoAutomaticRuns(unittest.IsolatedAsyncioTestCase):
    async def test_media_completion_does_not_check_new_or_edited_posts(self):
        edited = checked_row(analysis_pending=True)
        edited["content_edited_at"] = NOW + timedelta(seconds=1)
        for row in (dict(latest_check=None), checked_row(), edited):
            with patch.object(submissions, "fetch_one", AsyncMock(return_value=row)), \
                 patch.object(submissions, "_run_check", AsyncMock()) as run:
                await submissions.recheck_after_image_text(uuid4())
            run.assert_not_awaited()

    async def test_rulebook_change_does_not_queue_rechecks(self):
        with patch.object(submissions, "recheck_open_submissions", AsyncMock()) as run:
            submissions.schedule_rulebook_recheck("A rule was edited")
        run.assert_not_awaited()

    async def test_reading_scheduled_sentiment_does_not_start_it(self):
        row = dict(id=uuid4(), status="SCHEDULED", trigger="MANUAL")
        with patch.object(jobs, "fetch_one", AsyncMock(return_value=row)), \
             patch.object(jobs, "promote_due", AsyncMock()) as promote, \
             patch.object(jobs, "resume_unfinished", AsyncMock()) as resume, \
             patch.object(jobs, "_to_model", return_value="stored"), \
             patch.object(jobs, "_with_feedback", AsyncMock(return_value="stored")), \
             patch.object(jobs, "_with_comparison", AsyncMock(return_value="stored")):
            result = await jobs.get_latest(uuid4())
        self.assertEqual(result, "stored")
        promote.assert_not_awaited()
        resume.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
