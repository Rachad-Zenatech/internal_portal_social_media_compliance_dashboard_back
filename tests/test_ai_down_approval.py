import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.compliance_model import ComplianceViolation
from models.compliance.submission_model import ReviewDecisionRequest
from services.compliance.evaluation import context_evaluator as ce
from services.compliance.evaluation import script_review_service as srs
from services.compliance.submissions import submission_service as ss
from tests.test_compliance_scoring import rule

USER = uuid4()
SUB = uuid4()


def finding(rule_obj, penalty=35, light="RED"):
    return ComplianceViolation(
        rule_id=rule_obj.id, rule_number=rule_obj.rule_number, title="t", category="c", severity=rule_obj.severity,
        traffic_light=light, flagged_terms=["guarantees accuracy"], explanation="matched", penalty=penalty,
    )


class UnreadableFindingTests(unittest.TestCase):
    def test_a_critical_rule_stays_a_violation_when_no_model_can_read_it(self):
        r = rule(severity="CRITICAL", traffic_light="RED", prohibited_terms=["x"], evaluation_mode="CONTEXT")
        f = finding(r)
        ce._apply(ce.Candidate(f, r, "p", "c"), None, None, False)
        self.assertEqual((f.status, f.penalty, f.traffic_light), ("VIOLATION", 35, "RED"))
        self.assertEqual(f.evaluation.source, "UNAVAILABLE")
        self.assertIn("critical", f.explanation)

    def test_other_rules_are_still_only_a_needs_review(self):
        r = rule(severity="HIGH", traffic_light="YELLOW", prohibited_terms=["x"], evaluation_mode="CONTEXT")
        f = finding(r, penalty=20, light="YELLOW")
        ce._apply(ce.Candidate(f, r, "p", "c"), None, None, False)
        self.assertEqual((f.status, f.penalty), ("NEEDS_REVIEW", 0))


class ApprovalGateTests(unittest.IsolatedAsyncioTestCase):
    def row(self, check):
        # Checked under the current rulebook, so approving doesn't first re-run it.
        check = {"rulebook_version": "v1", **check}
        return {"id": SUB, "status": "IN_REVIEW", "created_by": uuid4(), "latest_check": check, "current_version": 1}

    async def approve(self, check, standing_result=None):
        standing = AsyncMock(return_value=standing_result or {})
        with patch.object(ss, "_get_owned_row", new=AsyncMock(return_value=self.row(check))),              patch.object(ss, "compliance_rulebook_version", new=AsyncMock(return_value="v1")), \
             patch.object(ss, "_can_review", new=AsyncMock(return_value=True)), \
             patch.object(ss, "_is_super_admin", new=AsyncMock(return_value=False)), \
             patch.object(ss.flag_review_service, "approval_standing", new=standing):
            with self.assertRaises(HTTPException) as err:
                await ss.decide_review(SUB, USER, ReviewDecisionRequest(decision="APPROVE"))
        return err.exception, standing

    async def test_no_approval_while_the_ai_review_could_not_run(self):
        err, standing = await self.approve({"ai_unavailable": True, "score": 100})
        self.assertEqual(err.status_code, 422)
        self.assertIn("AI review couldn't run", err.detail)
        standing.assert_not_awaited()

    async def test_no_approval_while_the_post_is_still_being_read(self):
        err, _ = await self.approve({"analysis_pending": True})
        self.assertEqual(err.status_code, 422)
        self.assertIn("still being read", err.detail)


    async def test_no_approval_while_part_of_the_post_could_not_be_read(self):
        failed = [{"what": "image text", "name": "poster.png", "error": "429 quota"}]
        err, standing = await self.approve({"score": 100, "reads_failed": failed})
        self.assertEqual(err.status_code, 422)
        self.assertIn("poster.png", err.detail)
        standing.assert_not_awaited()

    async def test_no_approval_while_a_flag_needs_a_persons_answer(self):
        from services.compliance.submissions.flag_review_service import ApprovalStanding

        err, _ = await self.approve({"score": 90}, {SUB: ApprovalStanding(open_critical=0, score=90, open_yellow=2)})
        self.assertEqual(err.status_code, 422)
        self.assertIn("2 flags need a person's answer", err.detail)

    async def test_a_check_from_an_older_rulebook_is_run_again_before_approving(self):
        stale = {"id": SUB, "status": "IN_REVIEW", "created_by": uuid4(), "current_version": 1,
                 "latest_check": {"rulebook_version": "old", "score": 100}}
        fresh = {**stale, "latest_check": {"rulebook_version": "v1", "ai_unavailable": True}}
        recheck = AsyncMock(return_value=(None, {}, False))
        with patch.object(ss, "_get_owned_row", new=AsyncMock(side_effect=[stale, fresh])),              patch.object(ss, "compliance_rulebook_version", new=AsyncMock(return_value="v1")),              patch.object(ss, "_recheck_one", new=recheck),              patch.object(ss, "_can_review", new=AsyncMock(return_value=True)),              patch.object(ss, "_is_super_admin", new=AsyncMock(return_value=False)):
            with self.assertRaises(HTTPException) as err:
                await ss.decide_review(SUB, USER, ReviewDecisionRequest(decision="APPROVE"))
        recheck.assert_awaited_once()
        self.assertIn("AI review couldn't run", err.exception.detail)


class ScriptApprovalGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_script_checked_without_the_ai_cannot_be_approved(self):
        from models.compliance.script_review_model import ScriptDecisionRequest

        sent = srs.ScriptVersion(version_number=1, body="Hello.", source="UPLOAD", created_at="2026-11-04T10:00:00Z", check={"ai_unavailable": True})
        row = {"id": SUB, "script_status": "IN_REVIEW", "script_required": True, "content_type": "VIDEO", "created_by": uuid4()}
        with patch.object(srs.subs, "_get_owned_row", new=AsyncMock(return_value=row)), \
             patch.object(srs, "_reviewer_may_act", new=AsyncMock(return_value=True)), \
             patch.object(srs, "_versions", new=AsyncMock(return_value=[sent])):
            with self.assertRaises(HTTPException) as err:
                await srs.decide(SUB, USER, ScriptDecisionRequest(decision="APPROVE"))
        self.assertEqual(err.exception.status_code, 422)
        self.assertIn("AI review couldn't run", err.exception.detail)

    async def approve_script(self, check):
        from models.compliance.script_review_model import ScriptDecisionRequest

        sent = srs.ScriptVersion(version_number=1, body="Hello.", source="UPLOAD", created_at="2026-11-04T10:00:00Z", check=check)
        row = {"id": SUB, "script_status": "IN_REVIEW", "script_required": True, "content_type": "VIDEO", "created_by": uuid4()}
        with patch.object(srs.subs, "_get_owned_row", new=AsyncMock(return_value=row)),              patch.object(srs, "_reviewer_may_act", new=AsyncMock(return_value=True)),              patch.object(srs, "_versions", new=AsyncMock(return_value=[sent])):
            with self.assertRaises(HTTPException) as err:
                await srs.decide(SUB, USER, ScriptDecisionRequest(decision="APPROVE"))
        return err.exception

    async def test_a_script_with_a_critical_flag_cannot_be_approved(self):
        err = await self.approve_script({"score": 40, "violations": [{"traffic_light": "RED"}]})
        self.assertEqual(err.status_code, 422)
        self.assertIn("critical compliance flag", err.detail)

    async def test_a_script_below_the_minimum_score_cannot_be_approved(self):
        err = await self.approve_script({"score": 60, "violations": [{"traffic_light": "YELLOW"}]})
        self.assertEqual(err.status_code, 422)
        self.assertIn("score is 60/100", err.detail)


class AiRecheckTests(unittest.IsolatedAsyncioTestCase):
    async def run_pass(self, results):
        rows = [{"id": uuid4(), "latest_check": {"ai_unavailable": True}} for _ in results]
        calls = AsyncMock(side_effect=[(None, {"ai_unavailable": down}, True) for down in results])
        with patch.object(ss, "fetch_all", new=AsyncMock(return_value=rows)), patch.object(ss, "_recheck_one", new=calls):
            read = await ss.recheck_ai_unavailable()
        return read, calls.await_count

    async def test_every_check_is_retried_once_the_ai_is_back(self):
        self.assertEqual(await self.run_pass([False, False, False]), (3, 3))

    async def test_it_stops_at_the_first_one_that_still_cannot_be_read(self):
        self.assertEqual(await self.run_pass([False, True, False]), (1, 2))

    async def test_nothing_to_retry_is_a_quiet_pass(self):
        self.assertEqual(await self.run_pass([]), (0, 0))


if __name__ == "__main__":
    unittest.main()
