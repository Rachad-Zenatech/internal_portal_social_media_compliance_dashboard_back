"""Authorized revisions do not need a new reviewer assignment or approval deadline."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.compliance.submissions import submission_service as service


class PublishRevisionValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_authorized_revisions_skip_reviewer_requirements(self):
        for status in ("CHANGES_REQUESTED", "DRAFT"):
            for permission in (False, True):
                row = dict(id=uuid4(), title="Post", brand_code="ZENADRONE", audience="PUBLIC",
                           region="Global", platforms=["LINKEDIN"], account_ids=[uuid4()],
                           rule_sets=["Rulebook"], approval_needed_by=None, reviewer_ids=[],
                           content_type="SOCIAL_POST", caption_text="Caption", status=status,
                           publish_after_changes=permission)
                with (
                    self.subTest(status=status, permission=permission),
                    patch.object(service, "fetch_one", new=AsyncMock(side_effect=[{"ok": 1}, None])),
                    patch.object(service, "script_stage_on", return_value=False),
                ):
                    missing = await service._missing_for_submit(row)
                    needs_review = not (status == "CHANGES_REQUESTED" and permission)
                    self.assertEqual("reviewer_ids" in missing, needs_review)
                    self.assertEqual("approval_needed_by" in missing, needs_review)

    async def test_authorized_revision_ignores_old_review_dates_and_assignments(self):
        user_id = uuid4()
        past = datetime.now(timezone.utc) - timedelta(days=1)
        row = dict(id=uuid4(), created_by=user_id, status="CHANGES_REQUESTED", publish_after_changes=True,
                   publish_at=past, approval_needed_by=past, reviewer_ids=[uuid4()], latest_check=None)
        with (
            patch.object(service, "_get_owned_row", new=AsyncMock(return_value=row)),
            patch.object(service, "_require_editable"),
            patch.object(service, "brands_of", return_value=["ZENADRONE"]),
            patch.object(service, "_missing_for_submit", new=AsyncMock(return_value=[])),
            patch.object(service, "_account_ids", return_value=[]),
            patch.object(service, "_connected_accounts", new=AsyncMock(return_value=[])),
            patch.object(service, "_require_not_duplicate", new=AsyncMock()),
            patch.object(service, "_creator_exclusion", new=AsyncMock()) as exclusion,
            patch.object(service, "compliance_rulebook_version", new=AsyncMock(return_value=1)),
            patch.object(service.manual_check_service, "require_current_check", side_effect=RuntimeError("reached compliance checks")),
            self.assertRaisesRegex(RuntimeError, "reached compliance checks"),
        ):
            await service.submit_for_review(row["id"], user_id)
        exclusion.assert_not_awaited()
