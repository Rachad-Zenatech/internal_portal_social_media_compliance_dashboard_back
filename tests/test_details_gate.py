"""The compliance check, sentiment analysis and public mood can't be run until the post's details are filled in."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from services.compliance.submissions import post_steps_service as ps
from services.compliance.submissions import submission_service as svc


def row(status="DRAFT"):
    return {"id": "x", "status": status}


class DetailsGateTests(unittest.IsolatedAsyncioTestCase):
    async def gate(self, missing, status="DRAFT"):
        with patch.object(svc, "_missing_for_submit", new=AsyncMock(return_value=missing)):
            await svc.require_details_complete(row(status), "running the compliance check")

    async def test_a_draft_with_missing_details_is_refused_and_told_what_is_missing(self):
        with self.assertRaises(HTTPException) as e:
            await self.gate(["title", "caption", "main_file"])
        self.assertEqual(e.exception.status_code, 422)
        self.assertIn("running the compliance check", e.exception.detail)
        self.assertIn("title, caption, image or video", e.exception.detail)

    async def test_a_complete_draft_is_let_through(self):
        await self.gate([])

    async def test_what_only_sending_needs_does_not_hold_the_tools_back(self):
        await self.gate(["approval_needed_by", "reviewer_ids"])

    async def test_a_sending_field_does_not_hide_a_real_gap(self):
        with self.assertRaises(HTTPException) as e:
            await self.gate(["reviewer_ids", "platforms"])
        self.assertIn("platform", e.exception.detail)
        self.assertNotIn("reviewer", e.exception.detail)

    async def test_changes_requested_is_held_to_the_same_rule(self):
        with self.assertRaises(HTTPException):
            await self.gate(["caption"], status="CHANGES_REQUESTED")

    async def test_a_post_that_was_sent_is_never_refused(self):
        for status in ("IN_REVIEW", "APPROVED", "REJECTED"):
            with self.subTest(status=status):
                await self.gate(["caption"], status=status)

    def test_every_missing_field_has_a_word(self):
        for field in ("title", "brand_code", "audience", "region", "platforms", "account_id", "rule_sets", "caption",
                      "main_file", "script_approval"):
            self.assertIn(field, ps.FIELD_WORDS)


if __name__ == "__main__":
    unittest.main()
