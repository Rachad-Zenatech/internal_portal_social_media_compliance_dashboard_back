import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.submission_model import SubmissionUpdate
from services.compliance.submissions import review_deadline_service
from services.compliance.evaluation import script_review_service
from services.compliance.submissions import submission_service

CREATOR = uuid4()
PICKED = uuid4()
OTHER_REVIEWER = uuid4()
SVC = "services.compliance.submissions.submission_service"


def submission_row(**overrides):
    row = {
        "id": uuid4(), "reference_code": "SUB-1001", "title": "Expo post", "status": "DRAFT", "created_by": CREATOR,
        "current_version": 0, "brand_code": "ZENADRONE", "reviewer_ids": [],
    }
    row.update(overrides)
    return row


class WhoMayOpenItTests(unittest.IsolatedAsyncioTestCase):
    async def load(self, row, user, *, super_admin=False):
        with (
            patch(f"{SVC}.fetch_one", new=AsyncMock(return_value=row)),
            patch(f"{SVC}._is_super_admin", new=AsyncMock(return_value=super_admin)),
            patch(f"{SVC}._can_see_team_content", new=AsyncMock(return_value=True)),
            patch(f"{SVC}._brand_scope", new=AsyncMock(return_value=None)),
        ):
            return await submission_service._get_owned_row(row["id"], user, manager_can_read=True)

    async def test_a_reviewer_it_was_sent_to_can_open_it(self):
        row = submission_row(status="IN_REVIEW", reviewer_ids=[PICKED])
        self.assertIs(await self.load(row, PICKED), row)

    async def test_other_reviewers_cant_open_it(self):
        with self.assertRaises(HTTPException) as caught:
            await self.load(submission_row(status="IN_REVIEW", reviewer_ids=[PICKED]), OTHER_REVIEWER)
        self.assertEqual(caught.exception.status_code, 404)

    async def test_content_sent_to_no_one_in_particular_is_open_to_every_reviewer(self):
        row = submission_row(status="IN_REVIEW")
        self.assertIs(await self.load(row, OTHER_REVIEWER), row)

    async def test_its_creator_and_super_admins_can_still_open_it(self):
        row = submission_row(status="IN_REVIEW", reviewer_ids=[PICKED])
        self.assertIs(await self.load(row, CREATOR), row)
        self.assertIs(await self.load(row, OTHER_REVIEWER, super_admin=True), row)

    def test_lists_and_queues_use_the_same_rule(self):
        self.assertEqual(submission_service._sent_to("$2"), "(cardinality(s.reviewer_ids) = 0 OR $2 = ANY(s.reviewer_ids))")


class WhoIsNotifiedTests(unittest.IsolatedAsyncioTestCase):
    async def recipients(self, row, everyone):
        with patch(f"{SVC}._reviewer_ids", new=AsyncMock(return_value=everyone)) as reviewer_ids:
            return await submission_service._review_recipients(row, exclude=CREATOR), reviewer_ids

    async def test_only_the_reviewers_it_was_sent_to(self):
        found, _ = await self.recipients(submission_row(reviewer_ids=[PICKED]), [PICKED, OTHER_REVIEWER])
        self.assertEqual(found, [PICKED])

    async def test_someone_who_can_no_longer_review_is_left_out(self):
        found, _ = await self.recipients(submission_row(reviewer_ids=[PICKED]), [OTHER_REVIEWER])
        self.assertEqual(found, [])

    async def test_every_reviewer_on_the_brand_when_nobody_was_picked(self):
        found, reviewer_ids = await self.recipients(submission_row(), [PICKED, OTHER_REVIEWER])
        self.assertEqual(found, [PICKED, OTHER_REVIEWER])
        reviewer_ids.assert_awaited_once_with(exclude=CREATOR, brand_codes=["ZENADRONE"])

    async def test_deadline_reminders_go_to_the_same_people(self):
        row = submission_row(due_kind="REVIEW", reviewer_ids=[PICKED])
        with patch(f"{SVC}._review_recipients", new=AsyncMock(return_value=[PICKED])) as recipients:
            self.assertEqual(await review_deadline_service._recipients(row), [PICKED])
        recipients.assert_awaited_once_with(row, exclude=CREATOR)

    async def test_a_script_goes_to_the_same_people(self):
        row = submission_row(reviewer_ids=[PICKED])
        with (
            patch(f"{SVC}._review_recipients", new=AsyncMock(return_value=[PICKED])),
            patch(f"{SVC}._notify", new=AsyncMock()) as notify,
        ):
            await script_review_service._send_to_reviewers(row, 1, {"score": 90, "violations_count": 0}, CREATOR)
        self.assertEqual(notify.await_args.args[0], [PICKED])


class PickingReviewersTests(unittest.IsolatedAsyncioTestCase):
    async def update(self, picked, eligible, super_admin=False):
        conn = MagicMock()
        conn.execute = AsyncMock()
        transaction = MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
        conn.transaction.return_value = transaction
        pool = MagicMock()
        pool.acquire.return_value = MagicMock(__aenter__=AsyncMock(return_value=conn), __aexit__=AsyncMock(return_value=False))
        with (
            patch(f"{SVC}._get_owned_row", new=AsyncMock(return_value=submission_row())),
            patch(f"{SVC}._reviewer_ids", new=AsyncMock(return_value=eligible)) as reviewer_ids,
            patch(f"{SVC}._is_super_admin", new=AsyncMock(return_value=super_admin)),
            patch(f"{SVC}.get_pool", return_value=pool),
            patch(f"{SVC}.get_submission", new=AsyncMock(return_value="updated")),
        ):
            await submission_service.update_submission(uuid4(), SubmissionUpdate(reviewer_ids=picked), CREATOR)
        return conn, reviewer_ids

    async def test_picked_reviewers_are_saved_once_each(self):
        conn, reviewer_ids = await self.update([PICKED, PICKED], [PICKED, OTHER_REVIEWER])

        sql, *params = conn.execute.await_args_list[0].args
        self.assertIn("reviewer_ids = $2", sql)
        self.assertEqual(params[1], [PICKED])
        reviewer_ids.assert_awaited_once_with(exclude=CREATOR, brand_codes=["ZENADRONE"])

    async def test_a_super_admin_can_pick_themselves(self):
        _, reviewer_ids = await self.update([CREATOR], [CREATOR, PICKED], super_admin=True)
        # Nobody is left out for being the creator.
        reviewer_ids.assert_awaited_once_with(exclude=None, brand_codes=["ZENADRONE"])

    async def test_someone_who_cant_review_the_brand_cant_be_picked(self):
        with self.assertRaises(HTTPException) as caught:
            await self.update([PICKED], [OTHER_REVIEWER])
        self.assertEqual(caught.exception.status_code, 422)

    async def test_clearing_the_choice_sends_it_to_every_reviewer_again(self):
        conn, _ = await self.update([], [PICKED])
        self.assertEqual(conn.execute.await_args_list[0].args[2], [])


class ReviewersAreRequiredTests(unittest.IsolatedAsyncioTestCase):
    async def missing(self, **overrides):
        row = submission_row(
            title="Expo post", audience="GENERAL", region="Global", platforms=["LINKEDIN"], account_ids=[uuid4()],
            rule_sets=["ZenaTech General"], approval_needed_by=datetime.now(timezone.utc) + timedelta(days=2),
            content_type="SOCIAL_POST", caption_text="Meet the team.", **overrides,
        )
        with patch(f"{SVC}.fetch_one", new=AsyncMock(return_value={"ok": 1})):
            return await submission_service._missing_for_submit(row)

    async def test_a_post_cant_be_sent_without_picking_reviewers(self):
        self.assertIn("reviewer_ids", await self.missing())
        self.assertNotIn("reviewer_ids", await self.missing(reviewer_ids=[PICKED]))

    def test_a_script_cant_be_sent_without_picking_reviewers(self):
        row = submission_row(
            title="Expo post", audience="GENERAL", region="Global", platforms=["LINKEDIN"], account_ids=[uuid4()],
            rule_sets=["ZenaTech General"], approval_needed_by=datetime.now(timezone.utc) + timedelta(days=2),
        )
        self.assertEqual(script_review_service._missing_details(row), ["Send to reviewers"])
        self.assertEqual(script_review_service._missing_details({**row, "reviewer_ids": [PICKED]}), [])


class ReviewerOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviewers_and_super_admins_are_offered_by_name(self):
        rows = [
            {"id": PICKED, "full_name": "Abc Reviewer", "email": "abc@zenatech.com", "is_super_admin": False},
            {"id": OTHER_REVIEWER, "full_name": None, "email": "boss@zenatech.com", "is_super_admin": True},
        ]
        with (
            patch(f"{SVC}._reviewer_ids", new=AsyncMock(return_value=[PICKED, OTHER_REVIEWER])) as reviewer_ids,
            patch(f"{SVC}._is_super_admin", new=AsyncMock(return_value=False)),
            patch(f"{SVC}.fetch_all", new=AsyncMock(return_value=rows)),
        ):
            options = await submission_service.reviewer_options(CREATOR, ["ZENADRONE"])

        reviewer_ids.assert_awaited_once_with(exclude=CREATOR, brand_codes=["ZENADRONE"])
        self.assertEqual([(o.name, o.is_super_admin) for o in options], [("Abc Reviewer", False), ("boss@zenatech.com", True)])

    async def test_a_super_admin_is_offered_to_themselves_too(self):
        with (
            patch(f"{SVC}._reviewer_ids", new=AsyncMock(return_value=[CREATOR])) as reviewer_ids,
            patch(f"{SVC}._is_super_admin", new=AsyncMock(return_value=True)),
            patch(f"{SVC}.fetch_all", new=AsyncMock(return_value=[])),
        ):
            await submission_service.reviewer_options(CREATOR, None)
        reviewer_ids.assert_awaited_once_with(exclude=None, brand_codes=None)


if __name__ == "__main__":
    unittest.main()
