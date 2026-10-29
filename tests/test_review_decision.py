import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.submission_model import ReviewDecisionRequest
from services.compliance import submission_service

CREATOR = uuid4()
REVIEWER = uuid4()
SVC = "services.compliance.submission_service"


def submission_row(manual_rules=(), **overrides):
    row = {
        "id": uuid4(),
        "reference_code": "SUB-1001",
        "title": "Expo post",
        "status": "IN_REVIEW",
        "created_by": CREATOR,
        "current_version": 1,
        "publish_at": None,
        "brand_code": "ZENADRONE",
        "latest_check": json.dumps(
            {
                "score": 100,
                "manual_review_rules": [
                    {"rule_id": str(uuid4()), "rule_number": number, "title": title, "severity": "HIGH"}
                    for number, title in manual_rules
                ],
            }
        ),
    }
    row.update(overrides)
    return row


def fake_pool():
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value="IN_REVIEW")
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction.return_value = transaction
    acquired = MagicMock()
    acquired.__aenter__ = AsyncMock(return_value=conn)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquired
    return pool, conn


def approve_now(**fields):
    return ReviewDecisionRequest(decision="APPROVE", publish_now=True, **fields)


class ReviewDecisionTestCase(unittest.IsolatedAsyncioTestCase):
    """Runs decide_review with the database and everything around it replaced."""

    async def decide(
        self,
        req,
        row,
        user=REVIEWER,
        *,
        is_reviewer=True,
        is_super=False,
        open_critical=0,
        approvers=((), ()),
        open_comments=0,
    ):
        pool, conn = fake_pool()

        async def fetch_one(sql, *args):
            return {"n": open_comments} if "count(*)" in sql else {"full_name": "Riley Reviewer"}

        with ExitStack() as stack:
            stack.enter_context(patch(f"{SVC}._get_owned_row", new=AsyncMock(return_value=row)))
            stack.enter_context(patch(f"{SVC}._can_review", new=AsyncMock(return_value=is_reviewer)))
            stack.enter_context(patch(f"{SVC}._is_super_admin", new=AsyncMock(return_value=is_super)))
            stack.enter_context(
                patch(
                    f"{SVC}.flag_review_service.open_critical_counts",
                    new=AsyncMock(return_value={row["id"]: open_critical}),
                )
            )
            stack.enter_context(patch(f"{SVC}.approval_path_service.load_rules", new=AsyncMock()))
            stack.enter_context(patch(f"{SVC}._approvers", return_value=(list(approvers[0]), list(approvers[1]))))
            stack.enter_context(patch(f"{SVC}._accounts", return_value=[]))
            stack.enter_context(patch(f"{SVC}.fetch_one", new=AsyncMock(side_effect=fetch_one)))
            stack.enter_context(patch(f"{SVC}.get_pool", return_value=pool))
            stack.enter_context(patch(f"{SVC}.log_audit_event", new=AsyncMock()))
            stack.enter_context(patch(f"{SVC}._notify", new=AsyncMock()))
            stack.enter_context(patch(f"{SVC}.review_memory_service.remember_soon"))
            stack.enter_context(patch(f"{SVC}.get_submission", new=AsyncMock(return_value="updated")))
            result = await submission_service.decide_review(row["id"], user, req)
        return result, conn

    def job_update(self, conn):
        """The arguments of the UPDATE that records the decision on the review job."""
        for call in conn.execute.await_args_list:
            if "UPDATE content_review_jobs" in call.args[0]:
                return call.args
        self.fail("The review job wasn't updated")

    async def assert_refused(self, status_code, req, row, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            await self.decide(req, row, **kwargs)
        self.assertEqual(caught.exception.status_code, status_code)
        return caught.exception.detail


class WhoMayDecideTests(ReviewDecisionTestCase):
    async def test_only_reviewers_can_decide(self):
        await self.assert_refused(403, approve_now(), submission_row(), is_reviewer=False)

    async def test_reviewer_cant_decide_on_their_own_content(self):
        await self.assert_refused(403, approve_now(), submission_row(created_by=REVIEWER))

    async def test_super_admin_can_approve_their_own_content(self):
        _, conn = await self.decide(approve_now(), submission_row(created_by=REVIEWER), is_super=True)
        self.assertEqual(self.job_update(conn)[3], "APPROVED")

    async def test_only_content_in_review_can_be_decided(self):
        await self.assert_refused(409, approve_now(), submission_row(status="APPROVED"))

    async def test_only_a_named_approver_can_give_final_approval(self):
        named = uuid4()
        detail = await self.assert_refused(
            403, approve_now(), submission_row(), approvers=([named], ["Named Approver"])
        )
        self.assertIn("Named Approver", detail)

        _, conn = await self.decide(approve_now(), submission_row(), user=named, approvers=([named], ["Named Approver"]))
        self.assertEqual(self.job_update(conn)[3], "APPROVED")

    async def test_any_reviewer_can_request_changes_on_a_named_path(self):
        req = ReviewDecisionRequest(decision="REQUEST_CHANGES", notes="Needs Legal's wording.")
        _, conn = await self.decide(req, submission_row(), approvers=([uuid4()], ["Named Approver"]))
        self.assertEqual(self.job_update(conn)[3], "CHANGES_REQUESTED")


class ApprovalRequirementTests(ReviewDecisionTestCase):
    async def test_open_critical_flag_blocks_approval(self):
        detail = await self.assert_refused(422, approve_now(), submission_row(), open_critical=1)
        self.assertIn("critical", detail)

    async def test_approving_needs_a_publish_time(self):
        await self.assert_refused(422, ReviewDecisionRequest(decision="APPROVE"), submission_row())

    async def test_publish_time_must_be_in_the_future(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        await self.assert_refused(422, ReviewDecisionRequest(decision="APPROVE", publish_at=past), submission_row())

    async def test_requesting_changes_needs_a_note_or_open_comment(self):
        await self.assert_refused(422, ReviewDecisionRequest(decision="REQUEST_CHANGES"), submission_row())

        _, conn = await self.decide(ReviewDecisionRequest(decision="REQUEST_CHANGES"), submission_row(), open_comments=2)
        self.assertEqual(self.job_update(conn)[3], "CHANGES_REQUESTED")


class ManualRuleChecklistTests(ReviewDecisionTestCase):
    MANUAL = (("Rule 20", "Image Editing"), ("Rule 21", "Image Sources, Rights and Releases"))

    async def test_approving_requires_confirming_every_rule_the_check_cant_test(self):
        detail = await self.assert_refused(
            422, approve_now(confirmed_rules=["Rule 20"]), submission_row(manual_rules=self.MANUAL)
        )
        self.assertIn("Rule 21", detail)
        self.assertNotIn("Rule 20 (", detail)

    async def test_confirmed_rules_are_recorded_with_the_approval(self):
        _, conn = await self.decide(
            approve_now(confirmed_rules=["Rule 20", "Rule 21"]), submission_row(manual_rules=self.MANUAL)
        )

        recorded = json.loads(self.job_update(conn)[-1])
        self.assertEqual(
            recorded,
            [
                {"rule_number": "Rule 20", "title": "Image Editing"},
                {"rule_number": "Rule 21", "title": "Image Sources, Rights and Releases"},
            ],
        )

    async def test_requesting_changes_needs_no_confirmations(self):
        req = ReviewDecisionRequest(decision="REQUEST_CHANGES", notes="Add the source.")
        _, conn = await self.decide(req, submission_row(manual_rules=self.MANUAL))
        self.assertIsNone(self.job_update(conn)[-1])

    async def test_checks_saved_before_the_checklist_need_no_confirmations(self):
        row = submission_row(latest_check=json.dumps({"score": 100}))
        _, conn = await self.decide(approve_now(), row)
        self.assertEqual(self.job_update(conn)[3], "APPROVED")


if __name__ == "__main__":
    unittest.main()
