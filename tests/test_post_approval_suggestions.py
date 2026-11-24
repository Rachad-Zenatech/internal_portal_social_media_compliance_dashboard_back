"""Approval remains final while other reviewers get a bounded suggestion window."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from services.compliance.submissions import post_approval_suggestions as policy
from services.compliance.submissions import submission_service as subs


class SuggestionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.creator, self.approver, self.other = uuid4(), uuid4(), uuid4()
        self.row = {"id": uuid4(), "created_by": self.creator, "status": "APPROVED",
                    "current_version": 2, "approval_needed_by": self.now + timedelta(hours=1), "publish_at": None}

    def test_other_reviewer_can_suggest_without_management_access(self):
        state = policy.access(self.row, self.other, True, self.approver, self.now)
        self.assertTrue(state["can_suggest_after_approval"])
        self.assertFalse(state["may_manage"])
        self.assertEqual(self.row["status"], "APPROVED")

    def test_suggestions_end_at_the_exact_deadline(self):
        for when in (self.row["approval_needed_by"], self.row["approval_needed_by"] + timedelta(seconds=1)):
            self.assertFalse(policy.access(self.row, self.other, True, self.approver, when)["can_suggest_after_approval"])

    def test_only_approver_retains_management(self):
        self.assertTrue(policy.access(self.row, self.approver, True, self.approver, self.now)["may_manage"])
        self.assertFalse(policy.access(self.row, self.approver, True, self.approver, self.now)["can_suggest_after_approval"])

    def test_no_permission_no_approval_or_no_deadline_does_not_grant_suggestions(self):
        self.assertFalse(policy.access(self.row, self.other, False, self.approver, self.now)["can_suggest_after_approval"])
        self.assertFalse(policy.access(self.row, self.other, True, None, self.now)["can_suggest_after_approval"])
        for changes in ({"status": "IN_REVIEW"}, {"approval_needed_by": None}):
            self.assertFalse(policy.access({**self.row, **changes}, self.other, True, self.approver, self.now)["can_suggest_after_approval"])

    def test_publishing_opens_at_deadline_even_if_intended_publish_date_is_earlier(self):
        self.assertFalse(policy.publishing_access(self.row, self.now).can_publish)
        self.assertTrue(policy.publishing_access(self.row, self.row["approval_needed_by"]).can_publish)
        self.assertFalse(policy.publishing_access({**self.row, "status": "IN_REVIEW"}, self.now).can_publish)

    def test_legacy_approved_post_without_deadline_can_publish(self):
        self.assertTrue(policy.publishing_access({**self.row, "approval_needed_by": None}, self.now).can_publish)

    def test_early_approval_does_not_force_a_publish_now_schedule(self):
        request = SimpleNamespace(publish_now=True, publish_at=None)
        when, label = policy.approval_timing(self.row, request, self.now)
        self.assertIsNone(when)
        self.assertIn("suggestions", label)

    def test_after_window_existing_approval_scheduling_validation_remains(self):
        row = {**self.row, "approval_needed_by": self.now - timedelta(seconds=1)}
        self.assertEqual(policy.approval_timing(row, SimpleNamespace(publish_now=True, publish_at=None), self.now)[0], self.now)
        with self.assertRaises(HTTPException):
            policy.approval_timing(row, SimpleNamespace(publish_now=False, publish_at=None), self.now)


class SuggestionPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_reviewers_cannot_publish_after_the_window_either(self):
        creator, other = uuid4(), uuid4()
        row = {"id": uuid4(), "status": "APPROVED", "created_by": creator,
               "approval_needed_by": datetime.now(timezone.utc) - timedelta(seconds=1)}
        with patch.object(subs, "_get_owned_row", new=AsyncMock(return_value=row)), \
             patch.object(subs, "_is_super_admin", new=AsyncMock(return_value=False)):
            self.assertFalse((await policy.publishing_for_viewer(row["id"], other)).can_publish)
            self.assertTrue((await policy.publishing_for_viewer(row["id"], creator)).can_publish)

    async def test_other_reviewer_cannot_reopen_or_reschedule_approved_post(self):
        approver, other, creator = uuid4(), uuid4(), uuid4()
        row = {"id": uuid4(), "status": "APPROVED", "created_by": creator}
        with patch.object(subs, "_get_owned_row", new=AsyncMock(return_value=row)), \
             patch.object(subs, "_can_review", new=AsyncMock(return_value=True)), \
             patch.object(policy, "approvals", new=AsyncMock(return_value={row["id"]: approver})):
            with self.assertRaises(HTTPException) as error:
                await subs._approved_row_for_reviewer(row["id"], other)
            self.assertEqual(error.exception.status_code, 403)
            self.assertEqual(await subs._approved_row_for_reviewer(row["id"], approver), row)

    async def test_closed_window_blocks_comment_access_for_other_reviewers(self):
        approver, other = uuid4(), uuid4()
        row = {"id": uuid4(), "status": "APPROVED", "created_by": uuid4(),
               "approval_needed_by": datetime.now(timezone.utc) - timedelta(seconds=1)}
        with patch.object(policy, "approvals", new=AsyncMock(return_value={row["id"]: approver})), \
             patch.object(subs, "_can_suggest", new=AsyncMock(return_value=True)):
            with self.assertRaises(HTTPException):
                await policy.require_comment(row, other)
            await policy.require_comment(row, approver)
            await policy.require_comment(row, row["created_by"])


if __name__ == "__main__":
    unittest.main()
