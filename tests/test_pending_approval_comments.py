"""Pending approvals retain reviewer comments until the suggestion deadline."""
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from services.compliance.submissions import post_approval_suggestions as service, review_handoff


class PendingApprovalCommentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_review_comment_permissions_follow_role_and_deadline(self):
        for own_post, can_suggest, is_super, expired, expected in (
            (False, True, False, False, True),
            (True, True, True, False, True),
            (True, True, False, False, False),
            (False, False, False, False, False),
            (True, True, True, True, False),
            (False, True, False, True, False),
        ):
            with self.subTest(own_post=own_post, super_admin=is_super, expired=expired):
                viewer = uuid4()
                row = dict(id=uuid4(), status="IN_REVIEW", created_by=viewer if own_post else uuid4(),
                           approval_needed_by=datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1))
                item = SimpleNamespace(can_review=True, can_approve=True)
                subs = types.ModuleType("services.compliance.submissions.submission_service")
                subs._can_suggest = AsyncMock(return_value=can_suggest)
                subs._is_super_admin = AsyncMock(return_value=is_super)
                with patch.dict(sys.modules, {subs.__name__: subs}), patch.object(
                    service, "approvals", AsyncMock(return_value={})
                ), patch.object(review_handoff, "decisions", AsyncMock(return_value={row["id"]: {}})):
                    await service.enrich([item], [row], viewer)
                self.assertEqual(item.can_suggest_after_approval, expected)
                self.assertTrue(item.review_decision_pending)
                self.assertFalse(item.can_review)
