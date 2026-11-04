import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from models.compliance.compliance_model import ComplianceViolation
from models.compliance.submission_model import ReviewDecisionRequest
from services.compliance import context_evaluator as ce
from services.compliance import script_review_service as srs
from services.compliance import submission_service as ss
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
        return {"id": SUB, "status": "IN_REVIEW", "created_by": uuid4(), "latest_check": check, "current_version": 1}

    async def approve(self, check):
        standing = AsyncMock(return_value={})
        with patch.object(ss, "_get_owned_row", new=AsyncMock(return_value=self.row(check))), \
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
