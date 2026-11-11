"""Scripts follow the same duplicate rules as posts: the same script, already sent for review for the same place and
publish date, is refused; for another date the creator confirms."""

import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.submission_model import DuplicateMatch, DuplicateReport
from services.compliance.evaluation import script_review_service as svc
from services.compliance.submissions import duplicate_service

DUP = "services.compliance.submissions.duplicate_service.find_script"


def match(**kw):
    base = dict(submission_id=uuid4(), reference_code="SUB-1050", title="Drone survey", status="IN_REVIEW",
                submitted_at=datetime(2026, 11, 1, tzinfo=timezone.utc), publish_at=None, created_by_name="Test Creator",
                details=[], all_overlap=True, same_date=True)
    base.update(kw)
    return DuplicateMatch(**base)


class NormalizeTests(unittest.TestCase):
    def test_case_and_spacing_dont_make_it_different(self):
        self.assertEqual(duplicate_service.normalize_script("  Hello\n\nWORLD  "),
                         duplicate_service.normalize_script("hello world"))


class RequireNotDuplicateTests(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, report, confirm=False):
        with patch(DUP, new=AsyncMock(return_value=report)):
            await svc._require_not_duplicate({"id": uuid4()}, "the script", confirm)

    async def test_same_place_and_date_is_refused(self):
        m = match()
        with self.assertRaises(HTTPException) as caught:
            await self.run_check(DuplicateReport(matches=[m], blocked=True, blocking=m))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "DUPLICATE_SCRIPT")
        self.assertIn("SUB-1050", caught.exception.detail["message"])

    async def test_another_date_needs_confirming(self):
        m = match(same_date=False)
        with self.assertRaises(HTTPException) as caught:
            await self.run_check(DuplicateReport(matches=[m], repost=m))
        self.assertEqual(caught.exception.detail["code"], "REPOST_CONFIRM")

    async def test_once_confirmed_it_goes(self):
        m = match(same_date=False)
        await self.run_check(DuplicateReport(matches=[m], repost=m), confirm=True)

    async def test_a_new_script_goes(self):
        await self.run_check(DuplicateReport())

    async def test_even_confirmed_a_true_duplicate_is_refused(self):
        m = match()
        with self.assertRaises(HTTPException):
            await self.run_check(DuplicateReport(matches=[m], blocked=True, blocking=m), confirm=True)


class ConfirmFlagTests(unittest.TestCase):
    def test_kept_with_the_send_in_progress(self):
        self.assertTrue(svc._repost_confirmed({"script_send": '{"status": "PROCESSING", "confirm_repost": true}'}))
        self.assertTrue(svc._repost_confirmed({"script_send": {"confirm_repost": True}}))
        self.assertFalse(svc._repost_confirmed({"script_send": None}))
        self.assertFalse(svc._repost_confirmed({"script_send": "not json"}))


if __name__ == "__main__":
    unittest.main()
