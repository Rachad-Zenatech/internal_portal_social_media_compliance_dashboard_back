import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.flag_review_model import FlagDismissRequest
from services.compliance import flag_review_service, submission_service

CREATOR = uuid4()
OTHER = uuid4()
SVC = "services.compliance.submission_service"


def submission_row(**overrides):
    row = {
        "id": uuid4(),
        "status": "IN_REVIEW",
        "created_by": CREATOR,
        "current_version": 1,
        "brand_code": "ZENADRONE",
        "title": "Expo post",
        "audience": "GENERAL",
        "region": "Global",
        "platforms": ["LINKEDIN"],
        "account_ids": [uuid4()],
        "rule_sets": ["ZenaTech General"],
        "caption_text": "Meet the team at the expo.",
        "content_type": "SOCIAL_POST",
        "latest_check": json.dumps({"violations": []}),
        "approval_needed_by": datetime.now(timezone.utc) + timedelta(days=2),
    }
    row.update(overrides)
    return row


class OwnedRowAccessTests(unittest.IsolatedAsyncioTestCase):
    async def load(self, row, user, *, super_admin=False, team=False, brands=None, manager_can_read=True):
        with (
            patch(f"{SVC}.fetch_one", new=AsyncMock(return_value=row)),
            patch(f"{SVC}._is_super_admin", new=AsyncMock(return_value=super_admin)),
            patch(f"{SVC}._can_see_team_content", new=AsyncMock(return_value=team)),
            patch(f"{SVC}._brand_scope", new=AsyncMock(return_value=brands)),
        ):
            return await submission_service._get_owned_row(row["id"], user, manager_can_read=manager_can_read)

    async def assert_hidden(self, row, user, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            await self.load(row, user, **kwargs)
        # Hidden content reads as missing rather than forbidden, so it doesn't reveal that it exists.
        self.assertEqual(caught.exception.status_code, 404)

    async def test_creator_sees_their_own_content(self):
        row = submission_row()
        self.assertIs(await self.load(row, CREATOR), row)

    async def test_creator_cant_see_someone_elses_content(self):
        await self.assert_hidden(submission_row(), OTHER)

    async def test_reviewer_sees_others_content_on_their_brands(self):
        row = submission_row(brand_code="ZENATECH")
        self.assertIs(await self.load(row, OTHER, team=True, brands=["ZENATECH"]), row)

    async def test_reviewer_cant_see_other_brands(self):
        await self.assert_hidden(submission_row(brand_code="ZENADRONE"), OTHER, team=True, brands=["ZENATECH"])

    async def test_reviewer_without_brand_limits_sees_every_brand(self):
        row = submission_row(brand_code="SOFTWARE")
        self.assertIs(await self.load(row, OTHER, team=True, brands=None), row)

    async def test_reviewers_only_see_others_content_where_the_screen_allows_it(self):
        await self.assert_hidden(submission_row(), OTHER, team=True, manager_can_read=False)

    async def test_deleted_content_is_never_returned(self):
        with patch(f"{SVC}.fetch_one", new=AsyncMock(return_value=None)):
            with self.assertRaises(HTTPException) as caught:
                await submission_service._get_owned_row(uuid4(), CREATOR)
        self.assertEqual(caught.exception.status_code, 404)


class SubmitRequirementTests(unittest.IsolatedAsyncioTestCase):
    async def missing(self, row, has_main_file=False):
        with patch(f"{SVC}.fetch_one", new=AsyncMock(return_value={"ok": 1} if has_main_file else None)):
            return await submission_service._missing_for_submit(row)

    async def test_a_complete_post_has_nothing_missing(self):
        self.assertEqual(await self.missing(submission_row()), [])

    async def test_every_missing_field_is_reported(self):
        row = submission_row(
            title=" ", brand_code=None, audience=None, region=None, platforms=[], account_ids=[], rule_sets=[],
            caption_text="", approval_needed_by=None,
        )
        self.assertEqual(
            set(await self.missing(row)),
            {"title", "brand_code", "audience", "region", "platforms", "account_id", "rule_sets", "caption",
             "approval_needed_by"},
        )

    async def test_image_posts_need_their_image(self):
        row = submission_row(content_type="IMAGE")
        self.assertEqual(await self.missing(row), ["main_file"])
        self.assertEqual(await self.missing(row, has_main_file=True), [])

    async def test_a_caption_is_needed_even_with_an_image(self):
        row = submission_row(content_type="IMAGE", caption_text="")
        self.assertEqual(await self.missing(row, has_main_file=True), ["caption"])


class FlagDismissalTests(unittest.IsolatedAsyncioTestCase):
    async def dismiss(self, row, user, *, reviewer=True, super_admin=False):
        with (
            patch(f"{SVC}._get_owned_row", new=AsyncMock(return_value=row)),
            patch(f"{SVC}._can_review", new=AsyncMock(return_value=reviewer)),
            patch(f"{SVC}._is_super_admin", new=AsyncMock(return_value=super_admin)),
        ):
            return await flag_review_service._reviewable(row["id"], user)

    async def assert_refused(self, status_code, row, user, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            await self.dismiss(row, user, **kwargs)
        self.assertEqual(caught.exception.status_code, status_code)

    async def test_reviewer_can_dismiss_flags_on_content_in_review(self):
        row = submission_row()
        self.assertIs(await self.dismiss(row, OTHER), row)

    async def test_creator_cant_dismiss_flags(self):
        await self.assert_refused(403, submission_row(), OTHER, reviewer=False)

    async def test_reviewer_cant_dismiss_flags_on_their_own_content(self):
        await self.assert_refused(403, submission_row(created_by=OTHER), OTHER)

    async def test_flags_can_only_be_dismissed_while_in_review(self):
        await self.assert_refused(409, submission_row(status="APPROVED"), OTHER)

    async def test_a_reason_of_other_needs_a_note(self):
        rule_id = uuid4()
        row = submission_row(latest_check=json.dumps({"violations": [{"rule_id": str(rule_id), "rule_number": "Rule 6"}]}))
        with (
            patch("services.compliance.flag_review_service._reviewable", new=AsyncMock(return_value=row)),
            self.assertRaises(HTTPException) as caught,
        ):
            await flag_review_service.dismiss(row["id"], rule_id, OTHER, FlagDismissRequest(reason="OTHER"))
        self.assertEqual(caught.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
