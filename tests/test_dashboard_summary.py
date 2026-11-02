import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from services.compliance import submission_service

SVC = "services.compliance.submission_service"
USER = uuid4()


class DashboardSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def summarise(self, *, see_all=True, brands=None, activity=(), decisions=None, average=None, findings=(), mine=False):
        fetch_all = AsyncMock(side_effect=[list(activity), list(findings)])
        fetch_one = AsyncMock(
            side_effect=[decisions or {"completed": 0, "approved": 0, "changes": 0, "overdue": 0}, {"average": average}]
        )
        with (
            patch(f"{SVC}._can_see_team_content", new=AsyncMock(return_value=see_all)),
            patch(f"{SVC}._brand_scope", new=AsyncMock(return_value=brands)),
            patch(f"{SVC}.fetch_all", new=fetch_all),
            patch(f"{SVC}.fetch_one", new=fetch_one),
        ):
            result = await submission_service.dashboard_summary(USER, mine=mine)
        return result, fetch_all, fetch_one

    async def test_every_query_is_limited_to_what_the_viewer_can_see(self):
        _, fetch_all, fetch_one = await self.summarise(see_all=True, brands=["ZENATECH"])

        for call in fetch_all.await_args_list + fetch_one.await_args_list:
            self.assertIn("s.created_by = $1", call.args[0])
            self.assertEqual(call.args[1:], (USER, True, ["ZENATECH"]))

    async def test_creators_only_see_their_own_content(self):
        _, fetch_all, _ = await self.summarise(see_all=False)
        self.assertEqual(fetch_all.await_args_list[0].args[1:], (USER, False, None))

    async def test_creator_view_narrows_a_reviewer_to_their_own_content(self):
        # A super admin viewing the app as a creator: only their own content, whatever they could see otherwise.
        _, fetch_all, fetch_one = await self.summarise(see_all=True, mine=True)
        for call in fetch_all.await_args_list + fetch_one.await_args_list:
            self.assertEqual(call.args[1:3], (USER, False))

    async def test_results_are_returned_as_the_dashboard_needs_them(self):
        submission_id = uuid4()
        result, _, _ = await self.summarise(
            activity=[
                {
                    "at": datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc),
                    "actor_id": USER,
                    "full_name": "Riley Reviewer",
                    "action": "approved",
                    "submission_id": submission_id,
                    "reference_code": "SUB-1071",
                    "title": "X post",
                }
            ],
            decisions={"completed": 5, "approved": 3, "changes": 2, "overdue": 1},
            average=88,
            findings=[{"rule_number": "Rule 25", "title": "Forward-Looking Statements", "count": 4}],
        )

        self.assertEqual(result.activity[0].action, "approved")
        self.assertEqual(result.activity[0].actor_name, "Riley Reviewer")
        self.assertEqual(
            (result.reviews_completed, result.approved, result.changes_requested, result.overdue_reviews),
            (5, 3, 2, 1),
        )
        self.assertEqual(result.average_score, 88)
        self.assertEqual(result.common_findings[0].count, 4)
        self.assertEqual(result.days, 30)

    async def test_no_content_sent_for_review_has_no_average(self):
        result, _, _ = await self.summarise(average=None)
        self.assertIsNone(result.average_score)
        self.assertEqual(result.activity, [])


class ReviewQueueScopeTests(unittest.IsolatedAsyncioTestCase):
    async def queue(self, mine):
        fetch_all = AsyncMock(return_value=[])
        with (
            patch(f"{SVC}._can_see_team_content", new=AsyncMock(return_value=True)),
            patch(f"{SVC}._can_review", new=AsyncMock(return_value=True)),
            patch(f"{SVC}._is_super_admin", new=AsyncMock(return_value=True)),
            patch(f"{SVC}._brand_scope", new=AsyncMock(return_value=None)),
            patch(f"{SVC}.fetch_all", new=fetch_all),
            patch(f"{SVC}.approval_path_service.load_rules", new=AsyncMock()),
            patch(f"{SVC}.flag_review_service.approval_standing", new=AsyncMock(return_value={})),
            patch(f"{SVC}.transcription_service.schedule_missing", new=AsyncMock()),
            patch(f"{SVC}.image_text_service.schedule_missing", new=AsyncMock()),
        ):
            await submission_service.review_queue(USER, "review", None, mine=mine)
        return fetch_all.await_args_list[0].args

    async def test_reviewers_see_everyones_content_by_default(self):
        args = await self.queue(mine=False)
        self.assertEqual(args[1:3], (USER, True))

    async def test_creator_view_shows_only_the_users_own_content(self):
        args = await self.queue(mine=True)
        self.assertEqual(args[1:3], (USER, False))


if __name__ == "__main__":
    unittest.main()
